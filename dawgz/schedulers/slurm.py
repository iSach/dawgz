r"""Slurm scheduling backend"""

from __future__ import annotations

import asyncio
import os
import shlex
import time

from pathlib import Path

from .core import (
    JobNeverSatisfiedError,
    JobSubmissionError,
    Scheduler,
)
from .. import payload, sacct, store
from ..runtime import runner
from ..utils import bytes_dump, trace
from ..workflow import Job, JobArray

# Most clusters limit arrays to 1001 tasks (MaxArraySize)
MAX_PACK = 1000


class SlurmScheduler(Scheduler):
    r"""Slurm scheduler.

    Jobs are submitted to the Slurm queue. Resources are allocated by the Slurm manager
    according to the job settings. Most settings (e.g. `account`, `export`, `partition`)
    are passed directly to `sbatch`. A few settings (e.g. `cpus`, `gpus`, `ram`) are
    translated into their `sbatch` equivalents.

    Independent jobs are submitted concurrently (at most `concurrency` `sbatch` calls at
    a time). Jobs whose dependencies can never be satisfied are cancelled by Slurm
    (`--kill-on-invalid-dep=yes`) instead of pending forever.
    """

    backend: str = "slurm"
    translate: dict[str, str] = {  # noqa: RUF012
        "tasks": "ntasks",
        "tasks_per_node": "ntasks-per-node",
        "cpus": "cpus-per-task",
        "gpus": "gpus-per-task",
        "ram": "mem",
        "memory": "mem",
        "timelimit": "time",
        "timeout": "time",
    }

    def __init__(
        self,
        name: str,
        concurrency: int | None = None,
        pack: bool | None = None,
        throttle: int | None = None,
    ) -> None:
        r"""
        Arguments:
            name: The name of the workflow.
            concurrency: The maximum number of simultaneous `sbatch` calls. If `None`,
                use the `DAWGZ_SBATCH_CONCURRENCY` environment variable or 16.
            pack: Whether to submit independent jobs with identical settings and
                dependencies as a single job array (one task per job). If `None`, use
                the `DAWGZ_PACK` environment variable or `True`.
            throttle: The maximum number of simultaneously running tasks of each pack
                (and of job arrays without their own throttle). For example, with
                `throttle=4`, a fan-out of 100 jobs runs 4 at a time.
        """

        super().__init__(name=name)

        if concurrency is None:
            concurrency = int(os.environ.get("DAWGZ_SBATCH_CONCURRENCY", 16))

        if pack is None:
            pack = os.environ.get("DAWGZ_PACK", "1") not in ("0", "false", "no")

        if throttle is not None and (not isinstance(throttle, int) or throttle < 1):
            raise ValueError(f"throttle should be a positive integer, got {throttle!r}")

        self.concurrency = max(concurrency, 1)
        self.pack = pack
        self.throttle = throttle
        self.scripts: dict[str, str] = {}

    def extra(self, job: Job) -> dict:
        script = getattr(self, "scripts", {}).get(self.tag(job))
        if script is None:
            return {}
        k = (
            self.results.get(job, "").rpartition("_")[2]
            if isinstance(self.results.get(job), str)
            else ""
        )
        return {"script": script, "stdout": script.replace(".sh", f"_{k}.out")}

    # Records

    def jobid(self, job: Job) -> str | None:
        result = self.results.get(job)
        return result if isinstance(result, str) else None

    def snapshot(self) -> dict:
        state = super().snapshot()
        now = time.time()

        for job, jobid in self.results.items():
            if job in self.order and isinstance(jobid, str):
                entry = {"state": "PENDING", "submit": now}
                if isinstance(job, JobArray):
                    entry["tasks"] = {}
                state["jobs"][str(self.order[job])] = entry

        state["source"] = "sbatch"

        return state

    def error_state(self, trace: str) -> str:
        return "CANCELLED" if "JobNeverSatisfiedError" in trace else "FAILED"

    def state(self, job: Job, i: int | None = None) -> str:
        if job not in self.order:
            return "UNKNOWN"

        view = store.Workflow.open(self.path)
        sacct.refresh([view])

        entry = view.jobs[self.order[job]]

        if i is not None:
            return view.entry(entry, i % entry["array"] if entry.get("array") else None)["state"]

        return view.summary(entry)["state"]

    def settings(self, job: Job, i: int | None = None) -> str | None:
        shfile = self.path / getattr(self, "scripts", {}).get(self.tag(job), f"{self.tag(job)}.sh")

        if shfile.exists():
            return shfile.read_text().strip("\n")
        else:
            return None

    # Submission

    def run(self, jobs: list[Job]) -> None:
        (self.path / "run.py").write_text(runner(import_paths()))

        units = self.units(jobs) if self.pack else [[job] for job in jobs]

        try:
            asyncio.run(self._submit_all(units))
        finally:
            # Also record partial submissions (e.g. interrupted by Ctrl-C)
            self.record()

    def units(self, jobs: list[Job]) -> list[list[Job]]:
        r"""Groups independent jobs with identical settings and dependencies into packs.

        A pack is submitted as a single job array, where each dawgz job is one array
        task. This reduces the number of `sbatch` calls (and the load on the Slurm
        controller) without changing the semantics of the workflow.
        """

        groups: dict[tuple, list[Job]] = {}
        units: list[list[Job]] = []

        for job in jobs:
            if isinstance(job, JobArray):
                units.append([job])
                continue

            key = (
                job.shell,
                job.interpreter,
                tuple(job.env),
                tuple(sorted(job.settings.items(), key=str)),
                tuple(sorted((self.order[d], s) for d, s in job.dependencies.items())),
                job.wait_mode,
                bool(job.satisfied),
                bool(job.unsatisfied),
            )

            if key in groups and len(groups[key]) < MAX_PACK:
                groups[key].append(job)
            else:
                groups[key] = [job]
                units.append(groups[key])

        return units

    async def _submit_all(self, units: list[list[Job]]) -> None:
        self.semaphore = asyncio.Semaphore(self.concurrency)
        self.unit_of = {job: (k, j) for k, unit in enumerate(units) for j, job in enumerate(unit)}
        self.tasks = [asyncio.ensure_future(self._submit(unit)) for unit in units]
        self.units_ = units

        try:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        finally:
            del self.tasks, self.semaphore, self.unit_of, self.units_

    async def _submit(self, unit: list[Job]) -> list[str]:
        try:
            jobids = await self._exec(unit)
        except BaseException as e:
            for job in unit:
                self.traces[job] = trace(e)
            raise
        else:
            for job, jobid in zip(unit, jobids, strict=True):
                self.results[job] = jobid
            return jobids

    async def _jobid(self, dep: Job) -> str | BaseException:
        k, j = self.unit_of[dep]
        try:
            return (await asyncio.shield(self.tasks[k]))[j]
        except BaseException as e:
            return e

    async def _exec(self, unit: list[Job]) -> list[str]:
        job = unit[0]

        # Dependencies (shared by all jobs of the unit)
        deps = list(job.dependencies.items())
        results = await asyncio.gather(*(self._jobid(dep) for dep, _ in deps))

        submitted = [
            (dep, status, jobid)
            for (dep, status), jobid in zip(deps, results, strict=True)
            if isinstance(jobid, str)
        ]
        failed = [
            dep
            for (dep, _), jobid in zip(deps, results, strict=True)
            if not isinstance(jobid, str)
        ]

        if job.wait_mode == "all":
            if failed or job.unsatisfied:
                raise JobNeverSatisfiedError(repr(job))
        elif job.satisfied:
            submitted = []  # already satisfied by a pruned dependency
        elif (deps or job.unsatisfied) and not submitted:
            raise JobNeverSatisfiedError(repr(job))

        name = self.unit_name(unit)
        loop = asyncio.get_running_loop()

        if len(unit) > 1:
            for j in unit:
                self.scripts[self.tag(j)] = f"{name}.sh"

        # Files
        shfile = self.path / f"{name}.sh"
        script = self.script(unit, submitted)
        await loop.run_in_executor(None, self._write_files, unit, name, shfile, script)

        # Submission
        async with self.semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "sbatch",
                    "--parsable",
                    str(shfile),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
            except OSError as e:
                raise JobSubmissionError(repr(job)) from e

        if proc.returncode != 0:
            error = RuntimeError(stderr.decode(errors="replace").strip("\n"))
            raise JobSubmissionError(repr(job)) from error

        jobid, *_ = stdout.decode().strip("\n").split(";")  # ignore cluster name

        if len(unit) > 1:
            return [f"{jobid}_{j}" for j in range(len(unit))]
        else:
            return [jobid]

    def unit_name(self, unit: list[Job]) -> str:
        return self.tag(unit[0]) + (".pack" if len(unit) > 1 else "")

    def _write_files(self, unit: list[Job], name: str, shfile: Path, script: str) -> None:
        job = unit[0]
        jobs = list(job.array) if isinstance(job, JobArray) else unit

        # Each distinct function is written once per workflow
        payload.write_functions(self.path, {j.fun_key for j in jobs})
        payloads = [payload.reference(j.fun_key, j.args_pkl) for j in jobs]

        if len(unit) > 1:
            store.write_json(self.path / f"{name}.json", [self.tag(j) for j in unit])

        if len(payloads) > 1 or isinstance(job, JobArray):
            with open(self.path / f"{name}.pkl", "wb") as f:
                bytes_dump(f, payloads)
        else:
            (self.path / f"{name}.pkl").write_bytes(payloads[0])

        shfile.write_text(script)

    def dependency(self, job: Job, deps: list[tuple[Job, str, str]]) -> str:
        after = {
            "success": "afterok",
            "failure": "afternotok",
            "any": "afterany",
        }

        items = []
        covered = set()

        if job.wait_mode == "all":
            # A dependency on all tasks of a pack is a dependency on the array
            by_unit: dict[int, list] = {}
            for dep, status, jobid in deps:
                by_unit.setdefault(self.unit_of[dep][0], []).append((dep, status, jobid))

            for k, items_k in by_unit.items():
                unit = self.units_[k]
                statuses = {status for _, status, _ in items_k}
                if (
                    len(unit) > 1
                    and len(items_k) == len(unit)
                    and statuses in ({"success"}, {"any"})
                ):
                    arrayid = items_k[0][2].split("_")[0]
                    items.append(f"{after[items_k[0][1]]}:{arrayid}")
                    covered.update(dep for dep, _, _ in items_k)

        for dep, status, jobid in deps:
            if dep not in covered:
                items.append(f"{after[status]}:{jobid}")

        return ("?" if job.wait_mode == "any" else ",").join(items)

    def script(self, unit: list[Job], deps: list[tuple[Job, str, str]]) -> str:
        job = unit[0]
        tag = self.tag(job)
        name = self.unit_name(unit)
        folder = str(self.path).replace("%", "%%")  # escape Slurm filename patterns

        if len(unit) > 1:
            logfile = f"{folder}/{name}_%a.out"
        elif isinstance(job, JobArray):
            logfile = f"{folder}/{tag}_%a.log"
        else:
            logfile = f"{folder}/{tag}.log"

        if " " in logfile:
            logfile = f'"{logfile}"'

        lines = [
            f"#!{job.shell}",
            "#",
            f"#SBATCH --job-name={tag}" + (f"+{len(unit) - 1}" if len(unit) > 1 else ""),
        ]

        throttle = getattr(self, "throttle", None)

        if len(unit) > 1:
            limit = f"%{throttle}" if throttle else ""
            lines.append(f"#SBATCH --array=0-{len(unit) - 1}{limit}")
        elif isinstance(job, JobArray):
            limit = job.throttle or throttle
            lines.append(f"#SBATCH --array=0-{len(job) - 1}" + (f"%{limit}" if limit else ""))

        lines.append(f"#SBATCH --output={logfile}")
        lines.append("#")

        ## Settings
        settings = {self.translate.get(k, k).replace("_", "-"): v for k, v in job.settings.items()}

        if "clusters" in settings:
            raise ValueError("multi-cluster jobs are not supported")

        for a, b in (
            ("ram", "memory"),
            ("time", "timeout"),
            ("time", "timelimit"),
            ("timeout", "timelimit"),
        ):
            if a in job.settings and b in job.settings:
                raise ValueError(f"conflicting settings '{a}' and '{b}' for job {job}")

        if "ntasks" not in settings:
            settings.setdefault("nodes", 1)
            settings.setdefault("ntasks-per-node", 1)

        if deps:
            settings.setdefault("kill-on-invalid-dep", "yes")

        for key, value in sorted(settings.items()):
            if value is True:
                lines.append(f"#SBATCH --{key}")
            elif value is False or value is None:
                continue
            else:
                lines.append(f"#SBATCH --{key}={value}")

        ## Dependencies
        if deps:
            lines.append("#")
            lines.append("#SBATCH --dependency=" + self.dependency(job, deps))

        lines.append("")

        ## Environment
        if job.env:
            lines.extend(job.env)
            lines.append("")

        ## Interpreter
        runner = shlex.quote(str(self.path / "run.py"))
        target = f"--pack {name}" if len(unit) > 1 else tag
        lines.append(f"srun {job.interpreter} {runner} {target}")
        lines.append("")

        return "\n".join(lines)


def import_paths() -> list[str]:
    r"""Returns the directories from which the jobs' modules should be importable."""

    import __main__

    paths = []
    main = getattr(__main__, "__file__", None)

    if main:
        paths.append(os.path.dirname(os.path.abspath(main)))

    paths.append(os.getcwd())

    return list(dict.fromkeys(paths))
