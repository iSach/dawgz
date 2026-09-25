r"""Directed Acyclic Workflow Graph Scheduling"""

__version__ = "2.7.0"

from .constants import get_dawgz_dir, set_dawgz_dir  # noqa: F401


def __getattr__(name: str) -> object:
    r"""Lazily loads the public API (PEP 562).

    Importing `dawgz` is kept cheap (no `asyncio`, `rich`, `cloudpickle` or
    `wonderwords`) so that the `dawgz` CLI starts fast. Submodules are only
    imported on first use.
    """

    if name in ("array", "job", "schedule"):
        from . import _api
    elif name in ("Job", "JobArray"):
        from . import workflow as _api
    elif name in ("AsyncScheduler", "DummyScheduler", "Scheduler", "SlurmScheduler"):
        from . import schedulers as _api
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(_api, name)
    globals()[name] = value

    return value
