r"""Local scheduling backends"""

from __future__ import annotations

import os
import pickle
import random
import signal
import sys
import time

from collections import deque
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
    ) -> None:
        r"""
        Arguments:
            name: The name of the workflow.
            workers: The maximum number of jobs (or array elements) running in parallel.
                If `None`, use all CPU cores.
            max_workers: Deprecated alias for `workers`.
        """

        super().__init__(name=name)

        if max_workers is not None:
            workers = max_workers

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
        queue: deque[tuple[Job, int | None]] = deque()
        running: dict[int, tuple[Job, int | None]] = {}
        remaining: dict[Job, int] = {}
        failures: dict[Job, int] = {}
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
                            started += 1
                            self._announce(job, started, len(jobs))
                            if isinstance(job, JobArray):
                                queue.extend((job, j) for j in range(len(job)))
                                remaining[job] = len(job)
                            else:
                                queue.append((job, None))
                                remaining[job] = 1
                            failures[job] = 0

                # Launch
                while queue and len(running) < self.workers:
                    job, i = queue.popleft()
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

                self._set(
                    job,
                    i,
                    state="COMPLETED" if code == 0 else "FAILED",
                    end=time.time(),
                    exit=f"{code}:0" if code >= 0 else f"0:{-code}",
                    error=run.get("error") if code else None,
                )

                remaining[job] -= 1
                failures[job] += code != 0

                if remaining[job] == 0:
                    if failures[job]:
                        outcomes[job] = "failure"
                        self._failed(job, i, run)
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

        if not _can_fork():  # pragma: no cover
            return self._spawn_process(data, logfile, runfile)

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

    def _spawn_process(
        self, data: bytes, logfile: object, runfile: object
    ) -> int:  # pragma: no cover
        import multiprocessing as mp

        process = mp.get_context("spawn").Process(target=_child, args=(data, logfile, runfile))
        process.start()
        return process.pid

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

    def _interrupt(self, running: dict, queue: deque, waiting: list) -> None:
        for pid in running:
            try:
                os.kill(pid, signal.SIGTERM)
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

        for entry in self.entries.values():
            for e in [entry, *entry.get("tasks", {}).values()]:
                if not store.is_terminal(e.get("state")):
                    e.update(state="CANCELLED", reason="interrupted", end=time.time())

        self._save(finished=True)


class AsyncScheduler(LocalScheduler):
    r"""Alias of the local scheduler, kept for backward compatibility.

    Unlike `LocalScheduler`, all independent jobs run in parallel by default.
    """

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

    run = Run(runfile)

    return execute(data, run, out_fd=fd, tee=True)


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
