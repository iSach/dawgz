r"""Abstract scheduler and shared helpers"""

from __future__ import annotations

import getpass
import os
import socket
import sys
import time

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import store
from ..constants import get_dawgz_dir
from ..utils import cat, human_uuid, slugify
from ..workflow import Job, JobArray, cycles, prune, topological


class Scheduler(ABC):
    r"""Abstract workflow scheduler.

    A scheduler records its workflow in a directory (see `dawgz.store`) as soon as it is
    called, such that it can be monitored while jobs are submitted or executed.
    """

    backend: str = None

    def __init__(self, name: str) -> None:
        r"""
        Arguments:
            name: The name of the workflow.
        """

        super().__init__()

        self.name = name
        self.date = datetime.now().replace(microsecond=0)
        self.uid = human_uuid()

        self.path = get_dawgz_dir() / self.uid
        self.path.mkdir(parents=True)

        # Jobs
        self.order: dict[Job, int] = {}
        self.traces: dict[Job, str] = {}
        self.results: dict[Job, Any] = {}
        self.quiet = True

    # Records

    def dump(self) -> None:
        r"""Pickles the scheduler, such that it can be recovered with `Scheduler.load`."""

        import cloudpickle

        with open(self.path / "dump.pkl", "wb") as f:
            cloudpickle.dump(self, f)

    @staticmethod
    def load(path: Path) -> Scheduler:
        import pickle

        with open(path / "dump.pkl", mode="rb") as f:
            return pickle.load(f)

    def register(self) -> None:
        store.register(
            self.path.parent,
            (self.name, self.uid, self.date, self.backend, len(self.order), len(self.traces)),
        )

    def describe(self) -> dict:
        r"""Returns the static description of the workflow (`workflow.json`)."""

        sources: dict[str, int] = {}
        jobs = []

        for job, i in sorted(self.order.items(), key=lambda x: x[1]):
            source = getattr(job[0] if isinstance(job, JobArray) else job, "source", "")
            if source not in sources:
                sources[source] = len(sources)

            entry = {
                "index": i,
                "tag": self.tag(job),
                "name": job.name,
                "input": repr(job),
                "array": len(job) if isinstance(job, JobArray) else None,
                "throttle": getattr(job, "throttle", None),
                "deps": [
                    [self.order[dep], status]
                    for dep, status in job.dependencies.items()
                    if dep in self.order
                ],
                "wait": job.wait_mode,
                "pruned": {"satisfied": len(job.satisfied), "unsatisfied": len(job.unsatisfied)},
                "jobid": self.jobid(job),
                "settings": dict(job.settings),
                "source": sources[source],
            }

            if isinstance(job, JobArray):
                entry["inputs"] = [repr(job[j]) for j in range(len(job))]

            entry.update(self.extra(job))

            jobs.append(entry)

        date = self.date if isinstance(self.date, datetime) else datetime.now()

        return {
            "format": store.FORMAT,
            "uid": self.uid,
            "name": self.name,
            "backend": self.backend,
            "date": date.isoformat(),
            "timestamp": date.timestamp(),
            "cwd": os.getcwd(),
            "argv": list(sys.argv),
            "host": socket.gethostname(),
            "user": _user(),
            "pid": os.getpid(),
            "pid_start": store.pid_start(os.getpid()),
            "jobs": jobs,
            "sources": list(sources),
        }

    def snapshot(self) -> dict:
        r"""Returns the states known by the scheduler (`state.json`)."""

        jobs = {}

        for job, trace in self.traces.items():
            if job in self.order:
                jobs[str(self.order[job])] = {
                    "state": self.error_state(trace),
                    "error": trace,
                    "end": time.time(),
                }

        return {
            "format": store.FORMAT,
            "updated": time.time(),
            "source": self.backend,
            "jobs": jobs,
        }

    def error_state(self, trace: str) -> str:
        return "CANCELLED" if "JobNeverSatisfiedError" in trace else "FAILED"

    def record(self, state: dict | None = None) -> None:
        store.write_json(self.path / "workflow.json", self.describe())

        with store.locked(self.path / "state.lock"):
            store.write_json(self.path / "state.json", state or self.snapshot())

    def view(self) -> store.Workflow:
        return store.Workflow(self.path, self.describe())

    def jobid(self, job: Job) -> str | None:
        return None

    def extra(self, job: Job) -> dict:
        return {}

    # Inspection

    def tag(self, job: Job) -> str:
        if job in self.order:
            i = self.order[job]
        else:
            i = self.order[job] = len(self.order)

        return f"{i:04d}_{slugify(job.name)}"

    def state(self, job: Job, i: int | None = None) -> str:
        if job not in self.order:
            return "UNKNOWN"

        view = self.view()
        entry = view.jobs[self.order[job]]

        if i is not None:
            return view.entry(entry, i % entry["array"] if entry.get("array") else None)["state"]

        return view.summary(entry)["state"]

    def logs(self, job: Job, i: int | None = None) -> str | None:
        tag = self.tag(job)

        if isinstance(job, JobArray):
            logfile = self.path / f"{tag}_{i}.log"
        else:
            logfile = self.path / f"{tag}.log"

        if logfile.exists():
            with open(logfile, newline="", errors="replace") as f:
                return cat(f.read(), -1).strip("\n")
        elif job in self.traces:
            return self.traces[job].strip("\n")
        else:
            return None

    def settings(self, job: Job, i: int | None = None) -> str | None:
        return None

    def cancel(self, job: Job | int | None = None, i: int | None = None) -> str:
        view = store.Workflow.open(self.path)

        if job is None:
            index = None
        elif isinstance(job, int):
            index = job
        else:
            index = self.order[job]

        return view.cancel(index, i)

    # Scheduling

    def __call__(self, *jobs: Job) -> None:
        for cycle in cycles(*jobs, backward=True):
            raise CyclicDependencyGraphError(" <- ".join(map(str, cycle)))

        jobs = prune(*dict.fromkeys(jobs))

        # Deterministic indices, dependencies first
        for job in topological(*jobs):
            self.tag(job)

        self.register()
        self.run(list(self.order))

    @abstractmethod
    def run(self, jobs: list[Job]) -> None:
        r"""Executes or submits jobs, given in topological order."""
        pass


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def readiness(job: Job, outcomes: dict[Job, str]) -> str:
    r"""Determines whether a job is `"ready"`, should `"wait"` or will `"never"` run.

    Arguments:
        job: A pruned job.
        outcomes: The outcomes (`"success"`, `"failure"` or `"cancelled"`) of finished jobs.
    """

    results = []

    for dep, status in job.dependencies.items():
        outcome = outcomes.get(dep)

        if outcome is None:
            results.append(None)
        elif outcome == "cancelled":
            results.append(False)
        else:
            results.append(status == "any" or status == outcome)

    if job.wait_mode == "all":
        if job.unsatisfied or False in results:
            return "never"
        elif all(results):
            return "ready"
    else:
        if job.satisfied or True in results:
            return "ready"
        elif not results and not job.unsatisfied:
            return "ready"
        elif None not in results:
            return "never"

    return "wait"


class CyclicDependencyGraphError(Exception):
    pass


class JobNeverSatisfiedError(Exception):
    pass


class JobFailedError(Exception):
    pass


class JobNotFailedError(Exception):
    pass


class JobSubmissionError(Exception):
    pass
