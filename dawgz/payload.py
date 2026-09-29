r"""Pickled job payloads, with each function pickled once and stored once.

`cloudpickle` pickles the functions of the main script by value, together with the
global variables they use. A job function that uses a global model or dataset
therefore embeds a copy of it. Functions are thus pickled separately from arguments
and interned by content, such that 1000 jobs of the same function share one copy in
memory and on disk.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import sys
import warnings

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

CALL = "dawgz-call-v1"  # (CALL, function bytes, arguments bytes)
REF = "dawgz-ref-v1"  # (REF, function key, arguments bytes), function in fn_<key>.pkl

FUNCTIONS: dict[str, bytes] = {}
WARNED: set[str] = set()


class LargeJobWarning(UserWarning):
    pass


def _limit() -> float:
    try:
        return float(os.environ.get("DAWGZ_PICKLE_WARN", 8)) * 2**20
    except ValueError:
        return 8 * 2**20


def intern(fun: Callable) -> str:
    r"""Pickles a function (with the globals it uses) and returns its content key."""

    import cloudpickle

    data = cloudpickle.dumps(fun)
    key = hashlib.sha1(data).hexdigest()[:20]
    FUNCTIONS.setdefault(key, data)

    if len(data) > _limit() and key not in WARNED:
        WARNED.add(key)
        warn_large(fun, len(data))

    return key


def arguments(args: tuple, kwargs: dict) -> bytes:
    import cloudpickle

    return cloudpickle.dumps((tuple(args), dict(kwargs)))


def inline(key: str, args: bytes) -> bytes:
    r"""A self-contained payload."""

    return pickle.dumps((CALL, FUNCTIONS[key], args), protocol=pickle.HIGHEST_PROTOCOL)


def reference(key: str, args: bytes) -> bytes:
    r"""A payload referring to `fn_<key>.pkl`, see `write_functions`."""

    return pickle.dumps((REF, key, args), protocol=pickle.HIGHEST_PROTOCOL)


def write_functions(path: Path, keys: set[str]) -> None:
    for key in keys:
        file = path / f"fn_{key}.pkl"
        if not file.exists():
            file.write_bytes(FUNCTIONS[key])


def resolve(data: bytes, path: str | Path | None = None) -> Callable[[], Any]:
    r"""Returns the callable of a payload (or of a legacy pickled callable)."""

    obj = pickle.loads(data)

    if isinstance(obj, tuple) and len(obj) == 3 and obj[0] in (CALL, REF):
        kind, fun, args = obj
        if kind == REF:
            with open(Path(path or ".") / f"fn_{fun}.pkl", "rb") as f:
                fun = f.read()
        args, kwargs = pickle.loads(args)
        return partial(pickle.loads(fun), *args, **kwargs)

    return obj


def size(value: object) -> int:
    import cloudpickle

    try:
        return len(cloudpickle.dumps(value))
    except Exception:
        return 0


def warn_large(fun: Callable, total: int) -> None:
    r"""Warns that jobs of a function capture large objects, naming the culprits."""

    import types

    names = set()
    stack = [getattr(fun, "__code__", None)]
    while stack:
        code = stack.pop()
        if code is None:
            continue
        names.update(code.co_names)
        stack.extend(c for c in code.co_consts if isinstance(c, types.CodeType))

    globals_ = getattr(fun, "__globals__", {})
    heavy = []
    for name in names:
        value = globals_.get(name)
        if value is None or isinstance(value, types.ModuleType | type | types.FunctionType):
            continue
        n = size(value)
        if n > total / 10:
            heavy.append((n, name))

    heavy.sort(reverse=True)
    culprits = ", ".join(f"'{name}' ({n / 2**20:.1f} MB)" for n, name in heavy[:3])
    name = getattr(fun, "__qualname__", repr(fun))

    message = f"each '{name}' job captures {total / 2**20:.1f} MB"
    if culprits:
        message += f", mostly the global {culprits}"
    message += (
        ". Global variables used by a job are pickled with it: create large objects inside"
        " the job or load them from files. Identical functions are stored once per workflow."
    )

    warnings.warn(message, LargeJobWarning, stacklevel=_stacklevel())


def _stacklevel() -> int:
    # Point at the user's code, outside of dawgz
    frame, level = sys._getframe(1), 1
    here = os.path.dirname(os.path.abspath(__file__))
    while frame is not None and os.path.abspath(frame.f_code.co_filename).startswith(here):
        frame, level = frame.f_back, level + 1
    return level
