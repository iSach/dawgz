r"""On-disk workflow records shared by the schedulers, the CLI and the TUI.

This module only depends on the standard library and is cheap to import, such that
monitoring never needs to import job code, pickles or third-party packages. The
layout of a dawgz directory is described in `docs/format.md`:

    workflows.csv                registry, one row per workflow
    <uid>/workflow.json          static description of the workflow
    <uid>/state.json             cached scheduler states (e.g. from `sacct`)
    <uid>/<tag>[_<i>].run.json   status and progress reported by the running job
    <uid>/<tag>[_<i>].log        job logs
"""

from __future__ import annotations

import csv
import json
import os
import socket
import time

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

FORMAT = 1

# States
DONE = "done"
RUNNING = "running"
PENDING = "pending"
FAILED = "failed"
CANCELLED = "cancelled"
UNKNOWN = "unknown"

CATEGORIES = {
    "COMPLETED": DONE,
    "RUNNING": RUNNING,
    "COMPLETING": RUNNING,
    "CONFIGURING": RUNNING,
    "STAGE_OUT": RUNNING,
    "SIGNALING": RUNNING,
    "RESIZING": RUNNING,
    "PENDING": PENDING,
    "REQUEUED": PENDING,
    "REQUEUE_FED": PENDING,
    "REQUEUE_HOLD": PENDING,
    "RESV_DEL_HOLD": PENDING,
    "SUSPENDED": PENDING,
    "STOPPED": PENDING,
    "SUBMITTING": PENDING,
    "FAILED": FAILED,
    "TIMEOUT": FAILED,
    "OUT_OF_MEMORY": FAILED,
    "NODE_FAIL": FAILED,
    "BOOT_FAIL": FAILED,
    "DEADLINE": FAILED,
    "SPECIAL_EXIT": FAILED,
    "CANCELLED": CANCELLED,
    "PREEMPTED": CANCELLED,
    "REVOKED": CANCELLED,
}

TERMINAL = frozenset(s for s, c in CATEGORIES.items() if c in (DONE, FAILED, CANCELLED))


def category(state: str | None) -> str:
    return CATEGORIES.get(state or "", UNKNOWN)


def is_terminal(state: str | None) -> bool:
    return state in TERMINAL


# Files


def read_json(path: str | Path) -> dict | None:
    r"""Reads a JSON file, returning `None` if it is missing or incomplete."""

    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return None


def write_json(path: str | Path, obj: Any) -> None:
    r"""Atomically writes a JSON file, such that readers never see partial content."""

    path = str(path)
    tmp = f"{path}.{os.getpid()}.tmp"

    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))

    os.replace(tmp, path)


@contextmanager
def locked(path: str | Path, blocking: bool = True) -> Iterator[bool]:
    r"""Holds an advisory lock on a file. Yields whether the lock was acquired."""

    try:
        import fcntl
    except ImportError:  # pragma: no cover
        yield True
        return

    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        yield True
        return

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            acquired = True
        except OSError:
            acquired = False

        yield acquired
    finally:
        os.close(fd)


# Registry

COLUMNS = ("name", "uid", "date", "backend", "jobs", "errors")


def registry(dawgz_dir: Path) -> list[dict[str, str]]:
    r"""Lists the workflows of a dawgz directory, oldest first."""

    try:
        with open(dawgz_dir / "workflows.csv", newline="") as f:
            rows = list(csv.reader(f))
    except OSError:
        return []

    return [
        dict(zip(COLUMNS, row, strict=False))
        for row in rows
        if len(row) >= 2 and valid_uid(row[1])
    ]


def valid_uid(uid: str) -> bool:
    r"""Workflow IDs are single path components (never empty, `.` or `..`)."""

    return bool(uid) and uid not in (".", "..") and "/" not in uid and "\\" not in uid


def register(dawgz_dir: Path, row: Iterable[Any]) -> None:
    dawgz_dir.mkdir(parents=True, exist_ok=True)

    with (
        locked(dawgz_dir / "workflows.lock"),
        open(dawgz_dir / "workflows.csv", mode="a", newline="") as f,
    ):
        csv.writer(f).writerow(row)

    remember_dir(dawgz_dir)


def rewrite_registry(dawgz_dir: Path, rows: list[dict[str, str]]) -> None:
    tmp = dawgz_dir / f"workflows.csv.{os.getpid()}.tmp"

    with open(tmp, mode="w", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow([row.get(c, "") for c in COLUMNS])

    os.replace(tmp, dawgz_dir / "workflows.csv")


def known_dirs_file() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local/state"
    )
    return Path(state) / "dawgz" / "dirs"


def remember_dir(dawgz_dir: Path) -> None:
    r"""Records a dawgz directory such that the TUI can list workflows across projects."""

    if os.environ.get("DAWGZ_NO_GLOBAL_REGISTRY"):
        return

    try:
        file = known_dirs_file()
        known = file.read_text().splitlines() if file.exists() else []

        if str(dawgz_dir) not in known:
            file.parent.mkdir(parents=True, exist_ok=True)
            with open(file, "a") as f:
                f.write(f"{dawgz_dir}\n")
    except OSError:
        pass


# Records


def run_file(path: Path, tag: str, i: int | None = None) -> Path:
    return path / (f"{tag}.run.json" if i is None else f"{tag}_{i}.run.json")


def log_file(path: Path, tag: str, i: int | None = None) -> Path:
    return path / (f"{tag}.log" if i is None else f"{tag}_{i}.log")


class Workflow:
    r"""Read-only view of a recorded workflow, merging every source of state.

    States are resolved in order of authority: terminal scheduler states (e.g. `TIMEOUT`
    from `sacct`), then states reported by the job itself (`*.run.json`), then cached
    non-terminal scheduler states, and finally `PENDING`.
    """

    def __init__(self, path: Path, meta: dict, row: dict | None = None) -> None:
        self.path = path
        self.meta = meta
        self.row = row or {}
        self.jobs: list[dict] = meta.get("jobs", [])
        self.cache: dict = {}
        self.runs: dict[str, dict] = {}
        self.reload()

    @classmethod
    def open(cls, path: Path, row: dict | None = None) -> Workflow | None:
        meta = read_json(path / "workflow.json")

        if meta is None and (path / "dump.pkl").exists():
            meta = migrate(path)

        if meta is None:
            return None

        return cls(path, meta, row)

    @property
    def uid(self) -> str:
        return self.meta.get("uid", self.path.name)

    @property
    def name(self) -> str:
        return self.meta.get("name", self.row.get("name", "?"))

    @property
    def backend(self) -> str:
        return self.meta.get("backend", self.row.get("backend", "?"))

    @property
    def timestamp(self) -> float:
        return float(self.meta.get("timestamp") or 0)

    @property
    def updated(self) -> float:
        return float(self.cache.get("updated") or 0)

    def reload(self) -> None:
        self.cache = read_json(self.path / "state.json") or {}
        self.runs = {}

        # A single directory scan instead of one stat per job
        try:
            with os.scandir(self.path) as it:
                names = [e.name for e in it if e.name.endswith(".run.json")]
        except OSError:
            names = []

        for name in names:
            run = read_json(self.path / name)
            if run is not None:
                self.runs[name[: -len(".run.json")]] = run

        self._check_alive()
        self._infer()

    def _infer(self) -> None:
        r"""Infers the readiness of Slurm jobs from the states of their dependencies.

        A job whose dependencies are unfinished is necessarily pending, and a job whose
        dependencies can never be satisfied is cancelled by Slurm. Neither needs a query.
        """

        self.inferred: dict[int, str] = {}

        if self.backend != "slurm":
            return

        outcomes: dict[int, str | None] = {}
        jobs = sorted(self.jobs, key=lambda j: j["index"])

        for _ in range(len(jobs) + 1):  # a single pass for topologically indexed jobs
            changed = False

            for job in jobs:
                status = readiness(job, outcomes)
                if self.inferred.get(job["index"]) != status:
                    self.inferred[job["index"]] = status
                    changed = True

                state = self.summary(job)["state"]
                terminal = all(is_terminal(e["state"]) for e in self.summary(job)["elements"])
                outcome = OUTCOMES.get(category(state)) if terminal else None
                if outcomes.get(job["index"]) != outcome:
                    outcomes[job["index"]] = outcome
                    changed = True

            if not changed:
                break

    def _check_alive(self) -> None:
        r"""Detects local workflows whose scheduler process died without cleaning up."""

        if self.backend not in ("local", "async", "dummy") or self.cache.get("finished"):
            return

        pid, host = self.meta.get("pid"), self.meta.get("host")

        if not pid or host != socket.gethostname() or pid_alive(pid, self.meta.get("pid_start")):
            return

        for entry in self.cache.get("jobs", {}).values():
            for e in [entry, *entry.get("tasks", {}).values()]:
                if not is_terminal(e.get("state")):
                    e["state"] = "CANCELLED"
                    e["reason"] = "scheduler exited"

        self.cache["finished"] = True

    def entry(self, job: dict, i: int | None = None) -> dict:
        r"""Returns the effective state entry of a job or array element."""

        cached = self.cache.get("jobs", {}).get(str(job["index"]), {})

        if i is not None:
            parent = cached
            cached = parent.get("tasks", {}).get(str(i)) or {}
            if not cached and parent.get("state") in TERMINAL:
                cached = {"state": parent["state"], "reason": parent.get("reason", "")}

        run = self.runs.get(job["tag"] if i is None else f"{job['tag']}_{i}")
        state = cached.get("state")

        if run is None or state in TERMINAL:
            merged = dict(cached)
        elif run.get("state") in TERMINAL or run.get("state") == "RUNNING":
            # Cached times are older than the job's own report
            stale = ("progress", "elapsed", "reason")
            merged = {**{k: v for k, v in cached.items() if k not in stale}}
            merged.update({k: v for k, v in run.items() if k != "progress"})
        else:
            merged = dict(cached)

        if run is not None and run.get("progress"):
            merged["progress"] = run["progress"]
        if run is not None and run.get("status"):
            merged["status"] = run["status"]

        merged.setdefault("state", "PENDING")

        if merged["state"] == "PENDING" and self.inferred:
            status = self.inferred.get(job["index"])
            if status == "never":
                if kills_invalid(job):
                    merged["state"] = "CANCELLED"
                merged["reason"] = "DependencyNeverSatisfied"
            elif status == "wait":
                merged["reason"] = "Dependency"

        return merged

    def elements(self, job: dict) -> list[dict]:
        if job.get("array"):
            return [self.entry(job, i) for i in range(job["array"])]
        else:
            return [self.entry(job)]

    def summary(self, job: dict) -> dict:
        r"""Summarizes a job: element state counts, progress fraction and aggregated state."""

        elements = self.elements(job)
        counts = dict.fromkeys((DONE, RUNNING, PENDING, FAILED, CANCELLED, UNKNOWN), 0)
        fraction = 0.0

        for e in elements:
            c = category(e["state"])
            counts[c] += 1

            if c in (DONE, FAILED, CANCELLED):
                fraction += 1.0
            elif c == RUNNING:
                fraction += progress_fraction(e) or 0.0

        if counts[FAILED]:
            state = "FAILED" if counts[RUNNING] + counts[PENDING] == 0 else "RUNNING"
        elif counts[RUNNING]:
            state = "RUNNING"
        elif counts[PENDING]:
            state = "PENDING"
        elif counts[CANCELLED]:
            state = "CANCELLED"
        elif counts[DONE]:
            state = "COMPLETED"
        else:
            state = "UNKNOWN"

        if len(elements) == 1:
            state = elements[0]["state"]

        return {
            "state": state,
            "counts": counts,
            "total": len(elements),
            "fraction": fraction / max(len(elements), 1),
            "elements": elements,
        }

    def totals(self) -> dict[str, int]:
        r"""Counts the job (or array element) states of the whole workflow."""

        counts = dict.fromkeys((DONE, RUNNING, PENDING, FAILED, CANCELLED, UNKNOWN), 0)
        for job in self.jobs:
            for key, value in self.summary(job)["counts"].items():
                counts[key] += value
        return counts

    def stale(self, ttl: float) -> list[dict]:
        r"""Returns the Slurm jobs whose state should be refreshed."""

        if self.backend != "slurm" or time.time() - self.updated < ttl:
            return []

        stale = []

        for job in self.jobs:
            if not job.get("jobid"):
                continue

            elements = self.elements(job)

            # Jobs waiting for unfinished dependencies are pending
            if self.inferred.get(job["index"]) == "wait" and all(
                e["state"] == "PENDING" for e in elements
            ):
                continue

            # States reported by jobs are provisional until Slurm confirms them (e.g. a
            # failing rank, or a process killed for exceeding its memory)
            if not all(is_terminal(e.get("state")) for e in self.cached(job)):
                stale.append(job)

        return stale

    def cached(self, job: dict) -> list[dict]:
        r"""Returns the entries of `state.json` only (without job reports)."""

        entry = self.cache.get("jobs", {}).get(str(job["index"]), {})

        if not job.get("array"):
            return [entry]

        tasks = entry.get("tasks", {})
        return [tasks.get(str(i)) or {"state": entry.get("state")} for i in range(job["array"])]

    def update(self, entries: dict[str, dict], now: float) -> None:
        r"""Merges fresh scheduler entries (keyed by job ID) into the state cache."""

        by_id = {job["jobid"]: job for job in self.jobs if job.get("jobid")}

        with locked(self.path / "state.lock"):
            cache = read_json(self.path / "state.json") or {"format": FORMAT, "jobs": {}}
            jobs = cache.setdefault("jobs", {})

            for jobid, job in by_id.items():
                key = str(job["index"])
                old = jobs.get(key, {})

                if job.get("array"):
                    tasks = dict(old.get("tasks", {}))
                    for i in range(job["array"]):
                        new = entries.get(f"{jobid}_{i}")
                        if new is not None:
                            tasks[str(i)] = new
                    parent = entries.get(jobid, {})
                    jobs[key] = {**old, **parent, "tasks": tasks}
                elif jobid in entries:
                    jobs[key] = {**old, **entries[jobid]}

            cache["updated"] = now
            cache["source"] = "sacct"
            write_json(self.path / "state.json", cache)

        self.reload()

    def cancel(self, index: int | None = None, i: int | None = None) -> str:
        r"""Cancels the workflow, one of its jobs or one array element."""

        if self.backend == "slurm":
            import subprocess

            jobs = self.jobs if index is None else [self.jobs[index]]
            jobids = []

            for job in jobs:
                if not job.get("jobid"):
                    continue
                elif i is not None and job.get("array"):
                    if not is_terminal(self.entry(job, i % job["array"])["state"]):
                        jobids.append(f"{job['jobid']}_{i % job['array']}")
                elif not all(is_terminal(e["state"]) for e in self.elements(job)):
                    jobids.append(job["jobid"])

            if not jobids:
                return "nothing to cancel"

            proc = subprocess.run(["scancel", "-v", *jobids], capture_output=True, text=True)

            # Force the next read to query Slurm
            with locked(self.path / "state.lock"):
                cache = read_json(self.path / "state.json") or {"format": FORMAT, "jobs": {}}
                cache["updated"] = 0
                write_json(self.path / "state.json", cache)

            self.reload()

            return (proc.stderr or proc.stdout).strip("\n")
        else:
            import signal

            pid, host = self.meta.get("pid"), self.meta.get("host")

            if index is not None:
                return f"cancelling single jobs is not supported by the '{self.backend}' backend"
            elif self.cache.get("finished") or not pid:
                return "nothing to cancel"
            elif host != socket.gethostname():
                return f"the workflow runs on '{host}', cancel it from there"
            elif not pid_alive(pid, self.meta.get("pid_start")):
                return "nothing to cancel"

            os.kill(pid, signal.SIGTERM)

            return f"sent SIGTERM to scheduler process {pid}"


OUTCOMES = {DONE: "success", FAILED: "failure", CANCELLED: "cancelled"}


def readiness(job: dict, outcomes: dict[int, str | None]) -> str:
    r"""Mirrors the dependency semantics of Slurm for recorded jobs."""

    pruned = job.get("pruned", {})
    results = []

    for dep, status in job.get("deps", []):
        outcome = outcomes.get(dep)
        if outcome is None:
            results.append(None)
        elif outcome == "cancelled":
            # Slurm: afterany and afternotok are satisfied by cancelled dependencies
            results.append(status in ("any", "failure"))
        else:
            results.append(status == "any" or status == outcome)

    if job.get("wait", "all") == "all":
        if pruned.get("unsatisfied") or False in results:
            return "never"
        elif all(results):
            return "ready"
    else:
        if pruned.get("satisfied") or True in results:
            return "ready"
        elif not results and not pruned.get("unsatisfied"):
            return "ready"
        elif None not in results:
            return "never"

    return "wait"


def kills_invalid(job: dict) -> bool:
    settings = job.get("settings", {})
    value = settings.get("kill_on_invalid_dep", settings.get("kill-on-invalid-dep", True))
    return value not in (False, "no", None)


def main_bar(entry: dict) -> dict | None:
    r"""Returns the most relevant progress bar of a job.

    That is the first unfinished bar with a total (the outermost of nested bars, or the
    current one of successive bars), or the most recent bar.
    """

    bars = entry.get("progress") or []

    for bar in bars:
        if bar.get("total") and bar.get("n", 0) < bar["total"]:
            return bar

    return max(bars, key=lambda b: b.get("t", 0), default=None)


def progress_fraction(entry: dict) -> float | None:
    bar = main_bar(entry)

    if bar is None or not bar.get("total"):
        return None

    return max(0.0, min(1.0, bar.get("n", 0) / bar["total"]))


def pid_start(pid: int) -> int | None:
    r"""Returns the start time of a process (Linux), to detect reused PIDs."""

    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def pid_alive(pid: int, start: int | None = None) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return start is None
    except OSError:
        return False

    return start is None or pid_start(pid) in (None, start)


def workflows(dawgz_dir: Path) -> list[tuple[dict, Path]]:
    return [(row, dawgz_dir / row["uid"]) for row in registry(dawgz_dir)]


def migrate(path: Path) -> dict | None:
    r"""Builds `workflow.json` (and `state.json`) for workflows recorded by dawgz < 3.

    This is the only monitoring path that loads a pickle, and only once per workflow.
    """

    try:
        from .schedulers.core import Scheduler

        scheduler = Scheduler.load(path)
        meta, state = scheduler.describe(), scheduler.snapshot()
    except Exception:
        return None

    try:
        write_json(path / "workflow.json", meta)
        if not (path / "state.json").exists():
            write_json(path / "state.json", state)
    except OSError:
        pass

    return meta
