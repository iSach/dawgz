r"""Directed Acyclic Workflow Graph Scheduling"""

__version__ = "3.0.0"

import importlib

from typing import TYPE_CHECKING

from .constants import get_dawgz_dir, set_dawgz_dir  # noqa: F401

# Attributes are imported on first access, such that `import dawgz` (and the CLI)
# stays fast, and job machinery is only loaded when needed.
_LAZY = {
    "job": "api",
    "array": "api",
    "schedule": "api",
    "JobFactory": "api",
    "Job": "workflow",
    "JobArray": "workflow",
    "Scheduler": "schedulers",
    "AsyncScheduler": "schedulers",
    "DummyScheduler": "schedulers",
    "LocalScheduler": "schedulers",
    "SlurmScheduler": "schedulers",
    "progress": "_progress",
    "Progress": "_progress",
    "status": "_progress",
}

_MODULES = (
    "api",
    "constants",
    "graph",
    "logs",
    "runtime",
    "sacct",
    "schedulers",
    "store",
    "term",
    "utils",
    "workflow",
)

__all__ = [*_LAZY, "get_dawgz_dir", "set_dawgz_dir"]


def __getattr__(name: str) -> object:
    if name in _LAZY:
        module = importlib.import_module(f".{_LAZY[name]}", __name__)
        for key, source in _LAZY.items():
            if source == _LAZY[name]:
                globals()[key] = getattr(module, key)
        return globals()[name]
    elif name in _MODULES:
        return importlib.import_module(f".{name}", __name__)

    raise AttributeError(f"module 'dawgz' has no attribute '{name}'")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from ._progress import Progress, progress, status  # noqa: F401
    from .api import JobFactory, array, job, schedule  # noqa: F401
    from .schedulers import (  # noqa: F401
        AsyncScheduler,
        DummyScheduler,
        LocalScheduler,
        Scheduler,
        SlurmScheduler,
    )
    from .workflow import Job, JobArray  # noqa: F401
