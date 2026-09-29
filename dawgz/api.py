r"""User-facing interface: `job`, `array` and `schedule`."""

from __future__ import annotations

import functools
import os
import sys

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Generic, Literal, ParamSpec, overload

from .workflow import Job, JobArray, get_source

P = ParamSpec("P")

BACKENDS = ("local", "slurm", "dummy", "async")


class JobFactory(Generic[P]):
    r"""A function decorated with `dawgz.job`. Calling it returns a `dawgz.Job`.

    Example:
        >>> @dawgz.job(cpus=4, time="1:00:00")
        ... def train(lr: float): ...
        >>> job = train(1e-3)  # a Job, the function is not executed
        >>> jobs = train.map([1e-2, 1e-3, 1e-4])  # a list of jobs
        >>> big = train.options(gpus=4)(1e-3)  # override settings
    """

    def __init__(
        self,
        fun: Callable[P, Any],
        *,
        name: str | None = None,
        shell: str | Path = "/bin/bash",
        interpreter: str | Path = "python",
        env: list[str] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        if not callable(fun):
            raise TypeError(f"{fun!r} is not a callable object")

        functools.update_wrapper(self, fun)

        self.fun = fun
        self.name = name
        self.shell = shell
        self.interpreter = interpreter
        self.env = list(env) if env else None
        self.settings = dict(settings or {})
        self._source: str | None = None

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Job:
        return self._job(args, kwargs)

    def _job(self, args: tuple, kwargs: dict, fun_key: str | None = None) -> Job:
        if self._source is None:
            self._source = get_source(self.fun)

        return Job(
            self.fun,
            args,
            kwargs,
            name=self.name,
            shell=self.shell,
            interpreter=self.interpreter,
            env=self.env,
            settings=self.settings,
            source=self._source,
            fun_key=fun_key,
        )

    def __get__(self, obj: object, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return functools.partial(self, obj)

    def __repr__(self) -> str:
        return f"<dawgz.job {getattr(self.fun, '__qualname__', self.fun)!s}>"

    def options(
        self,
        *,
        name: str | None = None,
        shell: str | Path | None = None,
        interpreter: str | Path | None = None,
        env: list[str] | None = None,
        **settings,
    ) -> JobFactory[P]:
        r"""Returns a copy of the factory with other options and additional settings.

        Example:
            >>> big = train.options(gpus=4, partition="gpu")(1e-3)
        """

        return JobFactory(
            self.fun,
            name=self.name if name is None else name,
            shell=self.shell if shell is None else shell,
            interpreter=self.interpreter if interpreter is None else interpreter,
            env=self.env if env is None else env,
            settings={**self.settings, **settings},
        )

    @overload
    def map(self, *iterables: Iterable[Any], array: Literal[False] = ...) -> list[Job]: ...

    @overload
    def map(
        self,
        *iterables: Iterable[Any],
        array: Literal[True],
        throttle: int | None = ...,
        name: str | None = ...,
    ) -> JobArray: ...

    def map(
        self,
        *iterables: Iterable[Any],
        array: bool = False,
        throttle: int | None = None,
        name: str | None = None,
    ) -> list[Job] | JobArray:
        r"""Creates one job per element of the iterables, like the built-in `map`.

        Arguments:
            iterables: Iterables of positional arguments, zipped together.
            array: Whether to group the jobs into a job array. On Slurm, arrays are
                submitted with a single `sbatch` call and can be throttled.
            throttle: The maximum number of simultaneously running array jobs.
            name: The name of the array.

        Example:
            >>> tasks = process.map(range(50))
            >>> merge().after(*tasks)
        """

        from . import payload

        # Nothing can change between these jobs: the function is pickled once
        key = payload.intern(self.fun)
        jobs = [self._job(args, {}, key) for args in zip(*iterables, strict=True)]

        if array or throttle is not None:
            return JobArray(*jobs, name=name, throttle=throttle)
        else:
            return jobs


@overload
def job(
    fun: Callable[P, Any],
    /,
    *,
    name: str | None = ...,
    shell: str | Path = ...,
    interpreter: str | Path = ...,
    env: list[str] | None = ...,
    settings: dict[str, Any] = ...,
    **kwargs,
) -> JobFactory[P]: ...


@overload
def job(
    *,
    name: str | None = ...,
    shell: str | Path = ...,
    interpreter: str | Path = ...,
    env: list[str] | None = ...,
    settings: dict[str, Any] = ...,
    **kwargs,
) -> Callable[[Callable[P, Any]], JobFactory[P]]: ...


def job(
    fun: Callable[P, Any] | None = None,
    /,
    *,
    name: str | None = None,
    shell: str | Path = "/bin/bash",
    interpreter: str | Path = "python",
    env: list[str] | None = None,
    settings: dict[str, int | float | bool | str] | None = None,
    **kwargs,
) -> JobFactory[P] | Callable[[Callable[P, Any]], JobFactory[P]]:
    r"""Decorator to capture the arguments of a function for later execution.

    Arguments:
        fun: A function.
        name: The job name. If `None`, use the function name instead.
        shell: The scripting shell.
        interpreter: The interpreter command. For example, `python`, `uv run` or `torchrun`.
        env: A sequence of shell commands to execute before the function is run. For example,
            exporting environment variables or loading modules.
        settings: The settings of the job, interpreted by the scheduler. Settings include
            the allocated resources (e.g. `cpus=4`, `ram="16GB"`), the estimated runtime
            (e.g. `time="03:14:15"`), the partition (e.g. `partition="gpu"`) and much
            more.
        kwargs: Additional keyword arguments added to `settings`.
    """

    settings = {**(settings or {}), **kwargs}

    def decorator(fun: Callable[P, Any]) -> JobFactory[P]:
        return JobFactory(
            fun,
            name=name,
            shell=shell,
            interpreter=interpreter,
            env=env,
            settings=settings,
        )

    if fun is None:
        return decorator
    else:
        return decorator(fun)


def array(*jobs: Job, name: str | None = None, throttle: int | None = None) -> JobArray:
    r"""Creates an array from a group of independent jobs.

    Arguments:
        jobs: A group of jobs. These jobs should not have dependencies or dependents.
        name: The job array name. If `None`, use the jobs' name if unique, and `"array"` otherwise.
        throttle: The maximum number of simultaneously running jobs in the array.
            Only affects the Slurm backend.
    """

    if len(jobs) == 1 and not isinstance(jobs[0], Job):
        jobs = tuple(jobs[0])

    return JobArray(*jobs, name=name, throttle=throttle)


def default_name() -> str:
    import __main__

    file = getattr(__main__, "__file__", None)

    return os.path.basename(file) if file else "interactive"


def schedule(
    *jobs: Job,
    backend: Literal["local", "slurm", "dummy", "async"] | None = None,
    name: str | None = None,
    quiet: bool = False,
    **kwargs,
) -> Any:
    r"""Schedules a group of jobs.

    The `local` backend executes jobs on the current machine, each in a fresh process.
    By default, jobs run one at a time (`workers=1`) in a deterministic order:
    dependencies first, then creation order. Use `workers=4` to run up to 4 jobs in
    parallel, or `workers=None` to use all cores. The `dummy` backend is equivalent,
    but only prints job names. Both ignore interpreter, environment, and resource
    settings. The `async` backend is an alias of `local`, kept for compatibility.

    The `slurm` backend submits jobs to the Slurm queue. Resources are allocated by the
    Slurm manager according to the job settings. Most settings (e.g. `account`,
    `export`, `partition`) are passed directly to `sbatch`. A few settings (e.g. `cpus`,
    `ram`, `timeout`) are translated into their `sbatch` equivalents.

    Regardless of the backend, jobs that are not pending, as determined by their
    completion status, will be pruned from the workflow.

    Arguments:
        jobs: A group of jobs describing a workflow.
        backend: The scheduling backend. If `None`, use the `DAWGZ_BACKEND` environment
            variable, or `"local"`. This allows to run the same script locally or on
            Slurm, e.g. `DAWGZ_BACKEND=slurm python train.py`.
        name: The worflow name. If `None`, use the caller's filename instead.
        quiet: Whether to hide progress messages and eventual job errors or not.
        kwargs: Keyword arguments passed to the scheduler's constructor. For example,
            `workers` for the `local` backend or `concurrency` for the `slurm` backend.

    Returns:
        The workflow scheduler.
    """

    from .schedulers import AsyncScheduler, DummyScheduler, LocalScheduler, SlurmScheduler

    backends = {
        "local": LocalScheduler,
        "async": AsyncScheduler,
        "dummy": DummyScheduler,
        "slurm": SlurmScheduler,
    }

    if backend is None:
        backend = os.environ.get("DAWGZ_BACKEND") or "local"

    if backend not in backends:
        raise ValueError(f"unknown backend '{backend}', expected one of {', '.join(backends)}")

    if len(jobs) == 1 and not isinstance(jobs[0], Job):
        jobs = tuple(jobs[0])

    if name is None:
        name = default_name()

    # Options of other backends are ignored, such that the backend can be switched freely
    import inspect

    cls = backends[backend]
    accepted = inspect.signature(cls.__init__).parameters
    others = {
        p for other in backends.values() for p in inspect.signature(other.__init__).parameters
    }
    kwargs = {k: v for k, v in kwargs.items() if k in accepted or k not in others}

    scheduler = cls(name=name, **kwargs)
    scheduler.quiet = quiet
    scheduler(*jobs)
    scheduler.dump()

    if not quiet:
        summarize(scheduler)

    return scheduler


def summarize(scheduler: Any) -> None:
    r"""Prints a short summary of a scheduled workflow and its errors to standard error."""

    from . import store, term

    term.set_color(term.supports_color(sys.stderr))

    rows = store.registry(scheduler.path.parent)
    index = next((k for k, r in enumerate(rows) if r["uid"] == scheduler.uid), len(rows) - 1)
    lines = []

    for job, trace in scheduler.traces.items():
        i = scheduler.order.get(job, "?")
        head = trace.strip("\n").splitlines()
        tail = head[-12:]
        lines.append(f"{term.glyph('failed')} {term.style(f'#{i} {job}', 'bold')}")
        if len(head) > len(tail):
            lines.append(term.style("  │ …", "gray"))
        lines.extend(term.style("  │ ", "gray") + line for line in tail)

    n = len(scheduler.order)
    ok = n - len(scheduler.traces)
    jobs = f"{n} job{'s' if n != 1 else ''}"
    hint = term.style(f"dawgz {index}", "bold")

    if scheduler.backend == "slurm":
        verb = f"submitted {ok}/{n} jobs" if scheduler.traces else f"submitted {jobs}"
        cat = "failed" if scheduler.traces else "done"
    else:
        verb = f"ran {jobs}, {len(scheduler.traces)} failed" if scheduler.traces else f"ran {jobs}"
        cat = "failed" if scheduler.traces else "done"

    lines.append(
        f"{term.glyph(cat)} {term.style(scheduler.name, 'bold')} "
        f"{term.style('(' + scheduler.uid + ')', 'gray')} {verb} · inspect with {hint}"
    )

    try:
        print("\n".join(lines), file=sys.stderr, flush=True)
    except (BrokenPipeError, ValueError):
        pass
