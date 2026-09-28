r"""Structured progress reporting from within jobs.

Inside a dawgz job, progress is written to the job's status file (at most once per
second), where `dawgz` and `dawgz-tui` display it as live progress bars. Nothing is
written to the logs. Outside of dawgz, a simple bar is drawn on interactive terminals.

    for batch in dawgz.progress(loader, desc="epoch 1"):
        ...

    with dawgz.Progress(total=100, desc="train") as bar:
        for step in range(100):
            bar.update(1, loss=0.42)
"""

from __future__ import annotations

import os
import sys
import time

from collections.abc import Iterable, Iterator
from typing import Any, Generic, TypeVar

T = TypeVar("T")


def _run() -> Any:
    from . import runtime

    return runtime.CURRENT


class Progress(Generic[T]):
    r"""A progress bar reported to dawgz monitors.

    Arguments:
        iterable: An optional iterable to wrap.
        total: The expected number of steps. If `None`, use `len(iterable)` if possible.
        desc: A short description, which also identifies the bar within the job.
        unit: The unit of steps.
    """

    def __init__(
        self,
        iterable: Iterable[T] | None = None,
        total: float | None = None,
        desc: str | None = None,
        unit: str = "it",
    ) -> None:
        if total is None and iterable is not None:
            try:
                total = len(iterable)  # type: ignore[arg-type]
            except TypeError:
                total = None

        self.key = f"p{id(self):x}"
        self.iterable = iterable
        self.total = total
        self.desc = desc or "progress"
        self.unit = unit
        self.n = 0
        self.postfix: dict[str, Any] = {}
        self.start = time.monotonic()
        self.run = _run()
        self.closed = False
        self._drawn = 0.0
        self._tty = os.environ.get("DAWGZ_TTY") == "1" or (
            self.run is None and getattr(sys.stderr, "isatty", lambda: False)()
        )
        self._report(force=True)

    def __iter__(self) -> Iterator[T]:
        assert self.iterable is not None, "Progress is not wrapping an iterable"

        try:
            for x in self.iterable:
                yield x
                self.update()
        finally:
            self.close()

    def __len__(self) -> int:
        return int(self.total or 0)

    def __enter__(self) -> Progress[T]:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def update(self, n: float = 1, **postfix: Any) -> None:
        r"""Advances the bar by `n` steps and optionally updates its postfix (e.g. `loss=0.1`)."""

        self.n += n
        if postfix:
            self.postfix.update(postfix)
        self._report()

    def set(self, n: float, **postfix: Any) -> None:
        r"""Sets the number of completed steps."""

        self.n = n
        if postfix:
            self.postfix.update(postfix)
        self._report()

    def set_postfix(self, **postfix: Any) -> None:
        self.postfix.update(postfix)
        self._report()

    def set_description(self, desc: str) -> None:
        self.desc = desc
        self._report(force=True)

    def close(self) -> None:
        if self.closed:
            return

        self.closed = True
        self._report(force=True)

        if self._tty:
            sys.stderr.write("\n")
            sys.stderr.flush()

    def _postfix(self) -> str:
        items = []
        for key, value in self.postfix.items():
            if isinstance(value, float):
                value = f"{value:.4g}"
            items.append(f"{key}={value}")
        return ", ".join(items)

    def _report(self, force: bool = False) -> None:
        elapsed = time.monotonic() - self.start
        rate = self.n / elapsed if elapsed > 0 and self.n else None

        if self.run is not None:
            fields = {"n": self.n, "total": self.total, "unit": self.unit}
            if rate is not None:
                fields["rate"] = rate
                if self.total:
                    fields["eta"] = max(self.total - self.n, 0) / rate
            if self.postfix:
                fields["postfix"] = self._postfix()
            self.run.bar(self.key, desc=self.desc, **fields)

        if self._tty:
            now = time.monotonic()
            if force or now - self._drawn >= 0.1:
                self._drawn = now
                self._draw(rate)

    def _draw(self, rate: float | None) -> None:
        if self.total:
            frac = min(max(self.n / self.total, 0.0), 1.0)
            width = 30
            filled = int(frac * width)
            bar = "━" * filled + ("╸" if filled < width else "") + " " * (width - filled - 1)
            text = f"{self.desc}: {100 * frac:3.0f}% {bar} {self.n:g}/{self.total:g}"
        else:
            text = f"{self.desc}: {self.n:g} {self.unit}"

        if rate is not None:
            text += f" [{rate:.2f} {self.unit}/s]"
        if self.postfix:
            text += f" {self._postfix()}"

        sys.stderr.write("\r" + text + "\x1b[K")
        sys.stderr.flush()


def progress(
    iterable: Iterable[T] | None = None,
    total: float | None = None,
    desc: str | None = None,
    unit: str = "it",
) -> Progress[T]:
    r"""Wraps an iterable (like `tqdm`) to report its progress to dawgz.

    Arguments:
        iterable: An optional iterable to wrap.
        total: The expected number of steps. If `None`, use `len(iterable)` if possible.
        desc: A short description, which also identifies the bar within the job.
        unit: The unit of steps.
    """

    return Progress(iterable, total=total, desc=desc, unit=unit)


def status(message: str) -> None:
    r"""Reports a short status message (e.g. `"loading data"`) to dawgz monitors."""

    run = _run()

    if run is not None:
        run.status(message)
    elif getattr(sys.stderr, "isatty", lambda: False)():
        print(message, file=sys.stderr)
