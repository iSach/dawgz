r"""Workflow graph components"""

from __future__ import annotations

import inspect
import itertools

from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from textwrap import dedent, indent
from typing import (
    Any,
    Literal,
    TypeVar,
)

from .utils import as_scalar, pretty

# Creation order, used to break ties when ordering jobs
COUNTER = itertools.count()


class Node:
    r"""Abstract graph node"""

    def __init__(self) -> None:
        self.children = {}
        self.parents = {}

    def add_child(self, node: Node, edge: Any | None = None) -> None:
        self.children[node] = edge
        node.parents[self] = edge

    def add_parent(self, node: Node, edge: Any | None = None) -> None:
        node.add_child(self, edge)

    def rm_child(self, node: Node) -> None:
        self.children.pop(node, None)
        node.parents.pop(self, None)

    def rm_parent(self, node: Node) -> None:
        node.rm_child(self)


class Job(Node):
    r"""Job node."""

    def __init__(
        self,
        fun: Callable | None,
        args: Sequence[Any] = (),
        kwargs: dict[str, Any] = {},  # noqa: B006
        *,
        name: str | None = None,
        shell: str | Path = "/bin/bash",
        interpreter: str | Path = "python",
        env: list[str] | None = None,
        settings: dict[str, int | float | bool | str] | None = None,
        source: str | None = None,
        fun_key: str | None = None,
    ) -> None:
        super().__init__()

        self.seq = next(COUNTER)

        # The function is pickled (with the globals it uses) once per distinct content
        if fun is None:
            self.fun_key, self.args_pkl = None, None
        else:
            from . import payload

            inspect.signature(fun).bind(*args, **kwargs)
            self.fun_key = payload.intern(fun) if fun_key is None else fun_key
            self.args_pkl = payload.arguments(args, kwargs)

        # Name
        if name is None:
            name = getattr(fun, "__name__", None)

        if not (isinstance(name, str) and name.replace("_", "").isalnum()):
            raise ValueError(
                f"job names can only contain underscores and alphanumeric characters, got '{name}'"
            )

        self.name = name

        # Input
        self.args_repr = [pretty(a) for a in args] + [
            f"{k}=" + pretty(v) for k, v in kwargs.items()
        ]

        # Source
        self.source = get_source(fun) if source is None else source

        # Settings
        self.shell = str(shell)
        self.interpreter = str(interpreter)

        if env:
            self.env = [str(cmd) for cmd in env]
        else:
            self.env = []

        if settings:
            self.settings = {str(k): as_scalar(v) for k, v in settings.items()}
        else:
            self.settings = {}

        # Status
        self.status: str = "pending"

        # Dependencies
        self.wait_mode: str = "all"
        self.satisfied: dict[Job, str] = {}
        self.unsatisfied: dict[Job, str] = {}

    def __repr__(self) -> str:
        prepr = f"{self.name}(" + ", ".join(self.args_repr) + ")"

        if "\n" in prepr or len(prepr) > 88:
            prepr = f"{self.name}(\n" + indent(",\n".join(self.args_repr), "  ") + "\n)"

        return prepr

    def __str__(self) -> str:
        return self.name

    @property
    def pkl(self) -> bytes | None:
        r"""The self-contained pickled payload of the job."""

        if getattr(self, "fun_key", None) is None:
            return None

        from . import payload

        return payload.inline(self.fun_key, self.args_pkl)

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state.pop("pkl", None)
        state.pop("args_pkl", None)
        return state

    def __setstate__(self, state: dict) -> None:
        state.setdefault("seq", 0)
        self.__dict__.update(state)

    def __rshift__(self, other: Job | Iterable[Job]) -> Job | list[Job]:
        r"""Declares that `other` runs after this job (`a >> b` is `b.after(a)`)."""

        if isinstance(other, Job):
            return other.after(self)

        others = list(other)
        for job in others:
            job.after(self)
        return others

    def __rrshift__(self, other: Iterable[Job]) -> Job:
        r"""Declares that this job runs after all jobs in `other` (`[a, b] >> c`)."""

        return self.after(*other)

    def mark(self, status: Literal["success", "failure", "cancelled", "pending"]) -> Job:
        r"""Sets the completion status of a job.

        Arguments:
            status: The completion status. The default status is `"pending"`.
        """
        _check("status", status, ("success", "failure", "cancelled", "pending"))
        self.status = status
        return self

    @property
    def dependencies(self) -> dict[Job, str]:
        return self.parents

    def after(self, *deps: Job, status: Literal["success", "failure", "any"] = "success") -> Job:
        r"""Adds dependencies to a job.

        Arguments:
            deps: A set of job dependencies.
            status: The desired dependency status.
        """
        _check("status", status, ("success", "failure", "any"))
        for dep in deps:
            if not isinstance(dep, Job):
                raise TypeError(f"dependencies should be jobs, got '{type(dep).__name__}'")
        for dep in deps:
            self.add_parent(dep, status)
        return self

    def detach(self, *deps: Job) -> None:
        for dep in deps:
            self.rm_parent(dep)

    def waitfor(self, mode: Literal["all", "any"]) -> Job:
        r"""Sets the waiting mode of a job.

        Arguments:
            mode: The dependency waiting mode. The default mode is `"all"`.
        """
        _check("mode", mode, ("all", "any"))
        self.wait_mode = mode
        return self

    @property
    def satisfy_status(self) -> Literal["ready", "never", "wait"]:
        if self.wait_mode == "all" and self.unsatisfied:
            return "never"
        elif self.wait_mode == "all" and not self.dependencies:  # noqa: SIM114
            return "ready"
        elif self.wait_mode == "any" and self.satisfied:
            return "ready"
        elif self.wait_mode == "any" and not self.dependencies and self.unsatisfied:
            return "never"
        elif not self.dependencies:
            return "ready"
        else:
            return "wait"


class JobArray(Job):
    def __init__(self, *jobs: Job, name: str | None = None, throttle: int | None = None) -> None:
        self.array = jobs
        self.throttle = throttle

        if len(self.array) < 1:
            raise ValueError("an array should contain at least one job")
        if len(self.array) != len(set(self.array)):
            raise ValueError("an array should not contain duplicates")
        if throttle is not None and (not isinstance(throttle, int) or throttle < 1):
            raise ValueError(f"throttle should be a positive integer, got {throttle!r}")

        if name is None:
            names = set(job.name for job in self.array)
            if len(names) > 1:
                name = "array"
            else:
                name = names.pop()

        super().__init__(
            fun=None,
            name=name,
            shell=self.array[0].shell,
            interpreter=self.array[0].interpreter,
            env=self.array[0].env,
            settings=self.array[0].settings,
        )

        for job in self.array:
            if job.parents:
                raise ValueError("jobs in an array should not have dependencies")
            if job.children:
                raise ValueError("jobs in an array should not have dependents")

        for key in ("shell", "interpreter", "env", "settings"):
            for job in self.array:
                if getattr(job, key) != getattr(self, key):
                    raise ValueError(f"all jobs in an array should have the same {key}")

    def __len__(self) -> int:
        return len(self.array)

    def __getitem__(self, i: int) -> Job:
        return self.array[i]

    def __repr__(self) -> str:
        return str(self)

    def __str__(self) -> str:
        if self.throttle is None:
            range = f"0-{len(self) - 1}"
        else:
            range = f"0-{len(self) - 1}%{self.throttle}"

        return f"{self.name}[{range}]"


def get_source(fun: Callable | None) -> str:
    try:
        return dedent(inspect.getsource(fun).strip("\n"))
    except (TypeError, OSError):
        return ""


def _check(name: str, value: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise ValueError(f"{name} should be one of {', '.join(map(repr, choices))}, got {value!r}")


N = TypeVar("N", bound=Node)


def dfs(*nodes: N, backward: bool = False) -> Iterator[N]:
    queue = list(nodes)
    visited = set()

    while queue:
        node = queue.pop()

        if node in visited:
            continue
        else:
            yield node

        queue.extend(node.parents if backward else node.children)
        visited.add(node)


def leafs(*nodes: N) -> set[N]:
    return {node for node in dfs(*nodes, backward=False) if not node.children}


def roots(*nodes: N) -> set[N]:
    return {node for node in dfs(*nodes, backward=True) if not node.parents}


def cycles(*nodes: Node, backward: bool = False) -> Iterator[list[Node]]:
    queue = [list(nodes)]
    path = []
    pathset = set()
    visited = set()

    while queue:
        branch = queue[-1]

        if not branch:
            if not path:
                break

            queue.pop()
            pathset.remove(path.pop())
            continue

        node = branch.pop()

        if node in visited:
            if node in pathset:
                yield path + [node]
            continue

        queue.append(list(node.parents if backward else node.children))
        path.append(node)
        pathset.add(node)
        visited.add(node)


def topological(*jobs: Job) -> list[Job]:
    r"""Orders jobs and their dependencies such that dependencies come first.

    Ties are broken by creation order, which makes the order deterministic and intuitive.
    """

    import heapq

    nodes = list(dfs(*jobs, backward=True))
    indegree = {node: len(node.parents) for node in nodes}
    heap = [(node.seq, i, node) for i, node in enumerate(nodes) if indegree[node] == 0]
    heapq.heapify(heap)
    rank = {node: i for i, node in enumerate(nodes)}
    order = []

    while heap:
        *_, node = heapq.heappop(heap)
        order.append(node)

        for child in node.children:
            if child in indegree:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(heap, (child.seq, rank[child], child))

    return order


def prune(*jobs: Job) -> list[Job]:
    for job in dfs(*jobs, backward=True):
        if job.status != "pending":
            job.detach(*job.dependencies)

        for dep, status in job.dependencies.items():
            if dep.status == "pending":
                pass
            elif dep.status == "cancelled":
                job.unsatisfied[dep] = status
            elif status == "any" or dep.status == status:
                job.satisfied[dep] = status
            else:
                job.unsatisfied[dep] = status

        job.detach(*job.satisfied, *job.unsatisfied)

    return list(dict.fromkeys(job for job in jobs if job.status == "pending"))
