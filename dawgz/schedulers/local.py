r"""Local scheduling backends"""

from __future__ import annotations

import heapq
import os
import pickle
import random
import signal
import sys
import time

from functools import partial

from .core import Scheduler, readiness
from .. import store, term
from ..workflow import Job, JobArray


class LocalScheduler(Scheduler):
    r"""Local scheduler.

    Jobs are executed on the current machine, each in a fresh process, as soon as their
    dependencies are satisfied. With `workers=1` (default), jobs run one at a time in a
    deterministic order: dependencies first, then creation order. The standard streams
    of jobs are shown in the terminal and written to their log files.
    """

    backend: str = "local"

    def __init__(
        self,
        name: str,
        workers: int | None = 1,
        max_workers: int | None = None,
        start: str | None = None,
    ) -> None:
        r"""
        Arguments:
            name: The name of the workflow.
            workers: The maximum number of jobs (or array elements) running in parallel.
                If `None`, use all CPU cores.
            max_workers: Deprecated alias for `workers`.
            start: How job processes are started. With `"fork"` (default on Linux), jobs
                inherit the modules already imported by the script, which makes them
                start instantly. With `"spawn"` (default on macOS), each job runs in a
                fresh interpreter, which is safer if the script uses threads.
        """

        super().__init__(name=name)

        if max_workers is not None:
            workers = max_workers

        if start is None:
            start = os.environ.get("DAWGZ_START") or ("fork" if _can_fork() else "spawn")

        if start not in ("fork", "spawn"):
            raise ValueError(f"start should be 'fork' or 'spawn', got {start!r}")

        self.start = start
        self.workers = workers or os.cpu_count() or 1
        self.max_workers = self.workers  # backward compatibility
        self.entries: dict[int, dict] = {}

    # Records

    def snapshot(self) -> dict:
        state = super().snapshot()
        state["jobs"].update({str(i): e for i, e in self.entries.items()})
        state["source"] = "local"
        return state

    def _save(self, finished: bool = False) -> None:
        state = self.snapshot()
        state["finished"] = finished
        try:
            with store.locked(self.path / "state.lock"):
                store.write_json(self.path / "state.json", state)
        except OSError:
            pass

    # Execution

    def payload(self, job: Job, i: int | None) -> bytes:
        return job[i].pkl if isinstance(job, JobArray) else job.pkl

    def run(self, jobs: list[Job]) -> None:
        for job in jobs:
            entry = self.entries[self.order[job]] = {"state": "PENDING"}
            if isinstance(job, JobArray):
                entry["tasks"] = {str(j): {"state": "PENDING"} for j in range(len(job))}

        self.record()

        outcomes: dict[Job, str] = {}
        waiting = list(jobs)
        # Ready jobs run by increasing index, i.e. in the order listed by `dawgz`
        queue: list[tuple[int, int, Job]] = []
        running: dict[int, tuple[Job, int | None]] = {}
        remaining: dict[Job, int] = {}
        failures: dict[Job, int] = {}
        first_failure: dict[Job, dict] = {}
        started = 0

        handler = None
        if _main_thread():
            handler = signal.signal(signal.SIGTERM, _terminate)

        try:
            while waiting or queue or running:
                # Resolve dependencies
                changed = True
                while changed:
                    changed = False
                    for job in list(waiting):
                        status = readiness(job, outcomes)

                        if status == "never":
                            waiting.remove(job)
                            outcomes[job] = "cancelled"
                            self._never(job)
                            changed = True
                        elif status == "ready":
                            waiting.remove(job)
                            k = self.order[job]
                            if isinstance(job, JobArray):
                                for j in range(len(job)):
                                    heapq.heappush(queue, (k, j, job))
                                remaining[job] = len(job)
                            else:
                                heapq.heappush(queue, (k, -1, job))
                                remaining[job] = 1
                            failures[job] = 0

                # Launch
                while queue and len(running) < self.workers:
                    _, j, job = heapq.heappop(queue)
                    i = None if j < 0 else j
                    if j <= 0:  # first (or only) element
                        started += 1
                        self._announce(job, started, len(jobs))
                    pid = self._spawn(job, i)
                    running[pid] = (job, i)
                    self._set(job, i, state="RUNNING", start=time.time())

                if running:
                    self._save()
                elif waiting:  # pragma: no cover (unreachable for acyclic graphs)
                    for job in waiting:
                        outcomes[job] = "cancelled"
                        self._never(job)
                    waiting.clear()
                    continue
                else:
                    break

                # Wait for any job to finish
                pid, status = os.waitpid(-1, 0)

                if pid not in running:
                    continue

                job, i = running.pop(pid)
                code = os.waitstatus_to_exitcode(status)
                run = store.read_json(store.run_file(self.path, self.tag(job), i)) or {}

                error = None
                if code != 0:
                    error = run.get("error") or describe_exit(code)
                    run.setdefault("error", error)

                self._set(
                    job,
                    i,
                    state="COMPLETED" if code == 0 else "FAILED",
                    end=time.time(),
                    exit=f"{code}:0" if code >= 0 else f"0:{-code}",
                    error=error,
                )

                remaining[job] -= 1
                failures[job] += code != 0
                if code != 0:
                    first_failure.setdefault(job, run)

                if remaining[job] == 0:
                    if failures[job]:
                        outcomes[job] = "failure"
                        self._failed(job, i, first_failure[job])
                    else:
                        outcomes[job] = "success"
                        self.results[job] = None
                    self._finish(job)
        except BaseException:
            self._interrupt(running, queue, waiting)
            raise
        finally:
            if handler is not None:
                signal.signal(signal.SIGTERM, handler)

        self._save(finished=True)

    def _spawn(self, job: Job, i: int | None) -> int:
        tag = self.tag(job)
        logfile = store.log_file(self.path, tag, i)
        runfile = store.run_file(self.path, tag, i)
        data = self.payload(job, i)
        tty = sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False

        for stream in (sys.stdout, sys.stderr):
            stream.flush()

        if getattr(self, "start", "fork") == "spawn":
            return self._spawn_process(data, logfile, runfile, tty)

        pid = os.fork()

        if pid == 0:  # child
            code = 1
            try:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.environ["DAWGZ_TTY"] = "1" if tty else "0"
                code = _child(data, logfile, runfile)
            finally:
                os._exit(code)

        return pid

    def _spawn_process(self, data: bytes, logfile: object, runfile: object, tty: bool) -> int:
        pklfile = str(runfile).replace(".run.json", ".local.pkl")
        with open(pklfile, "wb") as f:
            f.write(data)

        code = "import sys; from dawgz.schedulers.local import _spawned; _spawned(*sys.argv[1:])"
        env = {**os.environ, "DAWGZ_TTY": "1" if tty else "0"}
        paths = [p for p in sys.path if p]
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))

        # Reaped with `os.waitpid`, like forked processes (no `Popen` bookkeeping)
        return os.posix_spawn(
            sys.executable,
            [sys.executable, "-c", code, pklfile, str(logfile), str(runfile)],
            env,
        )

    def _set(self, job: Job, i: int | None, **fields) -> None:
        entry = self.entries[self.order[job]]
        fields = {k: v for k, v in fields.items() if v is not None}

        if i is None:
            entry.update(fields)
        else:
            entry["tasks"][str(i)].update(fields)
            states = [t["state"] for t in entry["tasks"].values()]
            if "RUNNING" in states:
                entry["state"] = "RUNNING"
            entry.setdefault("start", fields.get("start"))

    def _finish(self, job: Job) -> None:
        entry = self.entries[self.order[job]]

        if isinstance(job, JobArray):
            states = [t["state"] for t in entry["tasks"].values()]
            entry["state"] = "FAILED" if "FAILED" in states else "COMPLETED"
            entry["end"] = time.time()

        self._save()

        if not self.quiet:
            elapsed = entry.get("end", time.time()) - (entry.get("start") or time.time())
            cat = "done" if entry["state"] == "COMPLETED" else "failed"
            text = f"{term.glyph(cat)} {job} {term.style('· ' + term.duration(elapsed), 'gray')}"
            if entry.get("error"):
                text += " " + term.style(entry["error"], "red")
            _eprint(text)

    def _failed(self, job: Job, i: int | None, run: dict) -> None:
        trace = run.get("trace") or run.get("error") or "unknown error"
        self.traces[job] = f"{trace}\n\nJobFailedError: {job!r}"

    def _never(self, job: Job) -> None:
        self.traces[job] = f"JobNeverSatisfiedError: {job!r}"
        entry = self.entries[self.order[job]]
        entry.update(state="CANCELLED", reason="dependency never satisfied", end=time.time())
        entry["error"] = self.traces[job]
        for task in entry.get("tasks", {}).values():
            task.update(state="CANCELLED")
        self._save()

        if not self.quiet:
            _eprint(
                f"{term.glyph('cancelled')} {job} {term.style('· dependency never satisfied', 'gray')}"
            )

    def _announce(self, job: Job, k: int, n: int) -> None:
        if not self.quiet:
            size = f" · {len(job)} tasks" if isinstance(job, JobArray) else ""
            _eprint(term.style(f"▶ [{k}/{n}] {job!r}{size}", "cyan"))

    def _interrupt(self, running: dict, queue: list, waiting: list) -> None:
        # Jobs, and the processes they started
        family = {pid: descendants(pid) for pid in running}

        for pid, children in family.items():
            for p in [pid, *children]:
                try:
                    os.kill(p, signal.SIGTERM)
                except OSError:
                    pass

        deadline = time.monotonic() + 5.0
        while running and time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                time.sleep(0.05)
            else:
                running.pop(pid, None)

        for pid in running:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

        for children in family.values():
            for p in children:
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass

        for entry in self.entries.values():
            for e in [entry, *entry.get("tasks", {}).values()]:
                if not store.is_terminal(e.get("state")):
                    e.update(state="CANCELLED", reason="interrupted", end=time.time())

        self._save(finished=True)


class AsyncScheduler(LocalScheduler):
    r"""Alias of the local scheduler, kept for backward compatibility (`max_workers`)."""

    backend: str = "async"

    def __init__(self, name: str, max_workers: int | None = 1, workers: int | None = None) -> None:
        super().__init__(name=name, workers=workers if workers is not None else max_workers)


class DummyScheduler(LocalScheduler):
    r"""Dummy local scheduler.

    Jobs are scheduled like with the local backend, but instead of executing them, their
    name is printed before and after a short (random) sleep time. Useful for debugging.
    """

    backend: str = "dummy"

    def __init__(
        self, name: str, workers: int | None = None, max_workers: int | None = None
    ) -> None:
        super().__init__(name=name, workers=workers, max_workers=max_workers)

    def payload(self, job: Job, i: int | None) -> bytes:
        return pickle.dumps(partial(_dummy, repr(job if i is None else job[i])))


def _dummy(name: str) -> None:
    print(f"START {name}")
    time.sleep(random.random())
    print(f"END   {name}")


def _child(data: bytes, logfile: object, runfile: object) -> int:
    from ..runtime import Run, execute

    fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    if os.environ.get("DAWGZ_RAW_LOGS", "") not in ("", "0"):
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        sys.stdout = open(1, "w", buffering=1, closefd=False)  # noqa: SIM115
        sys.stderr = open(2, "w", buffering=1, closefd=False)  # noqa: SIM115

    run = Run(runfile)

    return execute(data, run, out_fd=fd, tee=True)


def _spawned(pklfile: str, logfile: str, runfile: str) -> None:
    with open(pklfile, "rb") as f:
        data = f.read()
    os.remove(pklfile)
    sys.exit(_child(data, logfile, runfile))


def descendants(pid: int) -> list[int]:
    r"""Lists the descendants of a process (Linux), such that they can be terminated."""

    parents: dict[int, list[int]] = {}

    try:
        entries = os.listdir("/proc")
    except OSError:
        return []

    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        parents.setdefault(ppid, []).append(int(entry))

    out, stack = [], [pid]
    while stack:
        for child in parents.get(stack.pop(), []):
            out.append(child)
            stack.append(child)

    return out


def describe_exit(code: int) -> str:
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"killed by {name}"
    return f"exited with code {code}"


class Terminated(Exception):
    pass


def _terminate(signum: int, frame: object) -> None:
    raise Terminated("terminated by SIGTERM")


def _main_thread() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()


def _can_fork() -> bool:
    return hasattr(os, "fork") and sys.platform != "darwin"


def _eprint(text: str) -> None:
    try:
        print(text, file=sys.stderr, flush=True)
    except (BrokenPipeError, ValueError):
        pass
