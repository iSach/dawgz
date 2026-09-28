#!/usr/bin/env python3
r"""A tiny fake Slurm for tests and demos. It never contacts a real cluster.

Symlinks named `sbatch`, `sacct`, `scancel`, `squeue` and `srun` dispatch to this file.
State lives in `$FAKESLURM_DIR` (required). Environment variables:

    FAKESLURM_DIR     state directory (required)
    FAKESLURM_EXEC    if 1, submitted jobs are actually executed (in the background)
    FAKESLURM_DELAY   latency of each command, in seconds (default 0)
    FAKESLURM_CPUS    maximum number of concurrently running tasks per job (default 4)
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"}


def root() -> Path:
    path = os.environ.get("FAKESLURM_DIR")
    if not path:
        sys.exit("fakeslurm: FAKESLURM_DIR must be set")
    path = Path(path)
    (path / "jobs").mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def lock() -> Iterator[None]:
    with open(root() / "lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load(jobid: str) -> dict | None:
    try:
        return json.loads((root() / "jobs" / f"{jobid}.json").read_text())
    except (OSError, ValueError):
        return None


def save(job: dict) -> None:
    path = root() / "jobs" / f"{job['id']}.json"
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(job))
    os.replace(tmp, path)


def log_call(argv: list[str]) -> None:
    with open(root() / "calls", "a") as f:
        f.write(shlex.join(argv) + "\n")


def delay() -> None:
    time.sleep(float(os.environ.get("FAKESLURM_DELAY", 0)))


# sbatch


def parse_directives(script: str) -> dict[str, str]:
    options = {}
    for line in script.splitlines():
        if not line.startswith("#SBATCH"):
            continue
        arg = line[len("#SBATCH") :].strip()
        if not arg.startswith("--"):
            continue
        key, _, value = arg[2:].partition("=")
        options[key] = value
    return options


def expand_array(spec: str) -> tuple[list[int], int | None]:
    spec, _, throttle = spec.partition("%")
    indices = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            indices.extend(range(int(a), int(b) + 1))
        elif part:
            indices.append(int(part))
    return indices, int(throttle) if throttle else None


def sbatch(argv: list[str]) -> None:
    parsable = "--parsable" in argv
    args = [a for a in argv if a != "--parsable"]
    script_path = args[-1]
    script = Path(script_path).read_text()
    options = parse_directives(script)

    for a in args[:-1]:
        if a.startswith("--"):
            key, _, value = a[2:].partition("=")
            options[key] = value

    if "fail" in options.get("partition", ""):
        sys.stderr.write("sbatch: error: invalid partition specified\n")
        sys.exit(1)

    with lock():
        counter = root() / "counter"
        jobid = int(counter.read_text()) + 1 if counter.exists() else 1000
        counter.write_text(str(jobid))

        array, throttle = (None, None)
        if "array" in options:
            array, throttle = expand_array(options["array"])

        job = {
            "id": str(jobid),
            "name": options.get("job-name", Path(script_path).name),
            "script": str(Path(script_path).resolve()),
            "output": options.get("output", f"slurm-{jobid}.out"),
            "array": array,
            "throttle": throttle,
            "dependency": options.get("dependency", ""),
            "kill_invalid": options.get("kill-on-invalid-dep", "no") == "yes",
            "time": options.get("time"),
            "submit": time.time(),
            "tasks": {str(i): {"state": "PENDING"} for i in array}
            if array
            else {"": {"state": "PENDING"}},
        }
        save(job)

    if os.environ.get("FAKESLURM_EXEC") == "1":
        subprocess.Popen(
            [sys.executable, os.path.realpath(__file__), "_exec", str(jobid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.getcwd(),
        )

    print(jobid if parsable else f"Submitted batch job {jobid}")


def dependency_status(spec: str) -> str:
    r"""Returns "ready", "wait" or "never"."""

    if not spec:
        return "ready"

    sep = "?" if "?" in spec else ","
    results = []

    for item in spec.split(sep):
        kind, _, ids = item.partition(":")
        for jobid in ids.split(":"):
            base, _, task = jobid.partition("_")
            dep = load(base)
            if dep is None:
                results.append(False)
                continue
            states = [t["state"] for k, t in dep["tasks"].items() if not task or k == task]
            if not all(s in TERMINAL for s in states):
                results.append(None)
            elif kind == "afterok":
                results.append(all(s == "COMPLETED" for s in states))
            elif kind == "afternotok":
                results.append(any(s != "COMPLETED" for s in states))
            else:
                results.append(True)

    if sep == ",":
        if False in results:
            return "never"
        return "ready" if all(results) else "wait"
    else:
        if True in results:
            return "ready"
        return "never" if None not in results else "wait"


def update_task(jobid: str, task: str, **fields) -> dict:
    with lock():
        job = load(jobid)
        job["tasks"][task].update(fields)
        save(job)
    return job


def execute(jobid: str) -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    while True:
        job = load(jobid)
        if all(t["state"] in TERMINAL for t in job["tasks"].values()):
            return
        status = dependency_status(job["dependency"])
        if status == "ready":
            break
        elif status == "never":
            with lock():
                job = load(jobid)
                for t in job["tasks"].values():
                    if t["state"] == "PENDING":
                        if job["kill_invalid"]:
                            t.update(
                                state="CANCELLED",
                                reason="DependencyNeverSatisfied",
                                end=time.time(),
                            )
                        else:
                            t["reason"] = "DependencyNeverSatisfied"
                save(job)
            return
        with lock():
            job = load(jobid)
            for t in job["tasks"].values():
                if t["state"] == "PENDING":
                    t["reason"] = "Dependency"
            save(job)
        time.sleep(0.1)

    with lock():
        job = load(jobid)
        for t in job["tasks"].values():
            if t["state"] == "PENDING":
                t["reason"] = "JobArrayTaskLimit" if job["throttle"] else "Resources"
        save(job)

    limit = job["throttle"] or int(os.environ.get("FAKESLURM_CPUS", 4))
    queue = list(job["tasks"])
    running: dict[str, subprocess.Popen] = {}

    while queue or running:
        job = load(jobid)

        # Cancelled tasks
        for task, proc in list(running.items()):
            if job["tasks"][task]["state"] == "CANCELLED":
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                proc.wait()
                del running[task]

        queue = [t for t in queue if job["tasks"][t]["state"] == "PENDING"]

        while queue and len(running) < limit:
            task = queue.pop(0)
            output = (
                job["output"].replace("%a", task or "0").replace("%A", jobid).replace("%j", jobid)
            )
            env = {
                **os.environ,
                "SLURM_JOB_ID": jobid,
                "SLURM_PROCID": "0",
                "SLURM_JOB_NAME": job["name"],
            }
            if task:
                env.update(SLURM_ARRAY_JOB_ID=jobid, SLURM_ARRAY_TASK_ID=task)
            out = open(output, "w")
            proc = subprocess.Popen(
                ["/bin/bash", job["script"]],
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
            out.close()
            running[task] = proc
            update_task(
                jobid, task, state="RUNNING", start=time.time(), node="fake-node1", reason=""
            )

        limit_s = parse_time(job.get("time"))
        for task, proc in list(running.items()):
            start = load(jobid)["tasks"][task].get("start") or time.time()
            if limit_s and proc.poll() is None and time.time() - start > limit_s:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
                del running[task]
                update_task(jobid, task, state="TIMEOUT", end=time.time(), exit="0:15")
                continue
            code = proc.poll()
            if code is not None:
                del running[task]
                state = "COMPLETED" if code == 0 else "FAILED"
                current = load(jobid)["tasks"][task]["state"]
                if current != "CANCELLED":
                    update_task(
                        jobid, task, state=state, end=time.time(), exit=f"{max(code, 0)}:0"
                    )

        time.sleep(0.05)


def parse_time(text: str | None) -> float | None:
    r"""Parses Slurm time limits (`MM`, `MM:SS`, `HH:MM:SS`, `D-HH:MM:SS`)."""

    if not text or text == "UNLIMITED":
        return None
    days, _, rest = text.rpartition("-")
    parts = [float(p) for p in rest.split(":")]
    if len(parts) == 1:
        seconds = 60 * parts[0]
    elif len(parts) == 2:
        seconds = 60 * parts[0] + parts[1]
    else:
        seconds = 3600 * parts[0] + 60 * parts[1] + parts[2]
    return seconds + 86400 * (int(days) if days else 0)


# sacct


def fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else "Unknown"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}-{h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


def fields_of(jobid: str, task: dict, job: dict) -> dict[str, str]:
    start, end = task.get("start"), task.get("end")
    elapsed = ((end or time.time()) - start) if start else 0
    return {
        "JobID": jobid,
        "State": task["state"] + (" by 1000" if task["state"] == "CANCELLED" else ""),
        "Reason": task.get("reason") or "None",
        "Start": fmt_time(start),
        "End": fmt_time(end),
        "Elapsed": fmt_duration(elapsed),
        "ExitCode": task.get("exit", "0:0"),
        "NodeList": task.get("node", "None assigned"),
        "Timelimit": job.get("time") or "UNLIMITED",
        "JobName": job["name"],
    }


def collapse(indices: list[int]) -> str:
    parts, start, prev = [], None, None
    for i in indices:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            parts.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = i
    if start is not None:
        parts.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(parts)


def sacct(argv: list[str]) -> None:
    ids, fields = [], ["JobID", "State"]
    it = iter(argv)
    for a in it:
        if a in ("-j", "--jobs"):
            ids = next(it).split(",")
        elif a.startswith("--jobs="):
            ids = a.split("=", 1)[1].split(",")
        elif a in ("-o", "--format"):
            fields = next(it).split(",")
        elif a.startswith("--format="):
            fields = a.split("=", 1)[1].split(",")

    for field in fields:
        if field not in fields_of("0", {"state": "PENDING"}, {"name": ""}):
            sys.stderr.write(f'sacct: error: Invalid field requested: "{field}"\n')
            sys.exit(1)

    lines = []
    for jobid in ids:
        base, _, only = jobid.partition("_")
        job = load(base)
        if job is None:
            continue
        if job["array"] is None:
            lines.append(fields_of(base, job["tasks"][""], job))
            continue
        pending = [
            int(t)
            for t, v in job["tasks"].items()
            if v["state"] == "PENDING" and not v.get("reason", "").startswith("DependencyNever")
        ]
        for t, v in job["tasks"].items():
            if only and t != only:
                continue
            if int(t) not in pending:
                lines.append(fields_of(f"{base}_{t}", v, job))
        if pending and not only:
            spec = collapse(sorted(pending))
            if job["throttle"]:
                spec += f"%{job['throttle']}"
            first = job["tasks"][str(pending[0])]
            lines.append(fields_of(f"{base}_[{spec}]", first, job))

    for row in lines:
        print("|".join(row[f] for f in fields))


# scancel


def scancel(argv: list[str]) -> None:
    ids = [a for a in argv if not a.startswith("-")]
    messages = []
    with lock():
        for jobid in ids:
            base, _, only = jobid.partition("_")
            job = load(base)
            if job is None:
                messages.append(f"scancel: error: Invalid job id {jobid}")
                continue
            for t, v in job["tasks"].items():
                if (not only or t == only) and v["state"] not in TERMINAL:
                    v.update(state="CANCELLED", end=time.time())
            save(job)
            messages.append(f"scancel: Terminating job {jobid}")
    if "-v" in argv:
        sys.stderr.write("\n".join(messages) + "\n")


def squeue(argv: list[str]) -> None:
    fmt = "%.18i %.9P %.8j %.8u %.2t %.10M %.6D %R"
    header = True
    it = iter(argv)
    for a in it:
        if a in ("-o", "--format"):
            fmt = next(it)
        elif a.startswith("--format="):
            fmt = a.split("=", 1)[1]
        elif a in ("-h", "--noheader"):
            header = False

    def fields(jobid: str, job: dict, task: dict) -> dict[str, str]:
        start = task.get("start")
        elapsed = fmt_duration(time.time() - start) if start else "0:00"
        state = task["state"]
        reason = task.get("reason") or ("None" if state == "RUNNING" else "Priority")
        return {
            "i": jobid,
            "P": "fake",
            "j": job["name"],
            "u": os.environ.get("USER", "user"),
            "T": state,
            "t": {"PENDING": "PD", "RUNNING": "R"}.get(state, state[:2]),
            "M": elapsed,
            "l": job.get("time") or "UNLIMITED",
            "D": "1",
            "R": task.get("node", "fake-node1") if state == "RUNNING" else f"({reason})",
        }

    rows = []
    for file in sorted((root() / "jobs").glob("*.json")):
        job = json.loads(file.read_text())
        pending = []
        for t, v in job["tasks"].items():
            if v["state"] == "RUNNING":
                rows.append(fields(job["id"] + (f"_{t}" if t else ""), job, v))
            elif v["state"] == "PENDING":
                pending.append((t, v))
        if pending:
            if job["array"] is None:
                rows.append(fields(job["id"], job, pending[0][1]))
            else:
                spec = collapse(sorted(int(t) for t, _ in pending))
                rows.append(fields(f"{job['id']}_[{spec}]", job, pending[0][1]))

    def render(row: dict[str, str]) -> str:
        return re.sub(r"%\.?\d*([a-zA-Z])", lambda m: row.get(m.group(1), ""), fmt)

    if header:
        print(render({k: k.upper() for k in "iPjutTMlDR"}))
    for row in rows:
        print(render(row))


def main() -> None:
    command = Path(sys.argv[0]).name
    argv = sys.argv[1:]

    if argv and argv[0] == "_exec":
        execute(argv[1])
        return

    if command == "srun":
        os.execvp(argv[0], argv)

    log_call([command, *argv])
    delay()

    {"sbatch": sbatch, "sacct": sacct, "scancel": scancel, "squeue": squeue}[command](argv)


if __name__ == "__main__":
    main()
