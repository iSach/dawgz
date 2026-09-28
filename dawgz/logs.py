r"""Bounded log reading, following and shrinking."""

from __future__ import annotations

import os
import sys
import time

from collections.abc import Callable
from pathlib import Path

from .utils import cat

CHUNK = 1 << 16


def read_log(path: Path) -> str:
    r"""Reads a whole log, as it would be displayed in a terminal."""

    with open(path, "rb") as f:
        return cat(f.read().decode(errors="replace"), -1).strip("\n")


def tail(path: Path, lines: int = 20, max_bytes: int = 1 << 22) -> str:
    r"""Reads the last lines of a log without reading the whole file."""

    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        position = end
        data = b""

        while position > 0 and data.count(b"\n") <= lines and end - position < max_bytes:
            step = min(CHUNK, position)
            position -= step
            f.seek(position)
            data = f.read(step) + data

    text = cat(data.decode(errors="replace"), -1).rstrip("\n")
    out = text.split("\n")

    if position > 0 and len(out) > lines:
        out = out[-lines:]
    elif position > 0:
        out = out[1:]  # first line may be partial

    return "\n".join(out[-lines:])


def follow(
    path: Path, lines: int = 20, done: Callable[[], bool] | None = None, interval: float = 0.5
) -> None:
    r"""Prints the last lines of a log and then its new content, like `tail -f`.

    Carriage-return redraws (progress bars) are rendered in place.
    """

    while not path.exists():
        if done is not None and done():
            return
        time.sleep(interval)

    print(tail(path, lines))

    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        last_check = time.monotonic()

        while True:
            data = f.read()

            if data:
                text = data.decode(errors="replace")
                sys.stdout.write(text.replace("\r\n", "\n"))
                sys.stdout.flush()
            else:
                if done is not None and time.monotonic() - last_check > 5:
                    last_check = time.monotonic()
                    if done():
                        return
                time.sleep(interval)


def shrink(path: Path, limit: int) -> int:
    r"""Truncates a log to about `limit` bytes, keeping its beginning and end.

    Carriage-return redraws are collapsed first, which is often enough.

    Returns:
        The number of bytes freed.
    """

    size = path.stat().st_size

    if size <= limit:
        return 0

    with open(path, "rb") as f:
        head = f.read(limit // 4)
        f.seek(max(size - (limit - limit // 4), 0))
        tail = f.read()

    head = cat(head.decode(errors="replace"), -1)
    tail = cat(tail.decode(errors="replace"), -1)

    marker = f"\n[dawgz: {size - limit} bytes truncated]\n"
    text = head.rsplit("\n", 1)[0] + marker + tail.split("\n", 1)[-1]

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, errors="replace")
    os.replace(tmp, path)

    return size - path.stat().st_size
