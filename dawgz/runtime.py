r"""Job runtime, executed where the job runs (Slurm node or local worker process).

The runtime

* reports the job status and progress in a small `<tag>[_<i>].run.json` file, which
  lets monitors know that a job is running or finished without querying Slurm;
* captures the standard streams at the file descriptor level and "cooks" them before
  they reach the log file: carriage-return redraws (e.g. `tqdm` bars) are collapsed and
  written at most every `DAWGZ_LOG_INTERVAL` seconds instead of ~10 times per second,
  which keeps logs small, and `tqdm` bars are parsed into structured progress.

Set `DAWGZ_RAW_LOGS=1` in the job environment to disable stream capture.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .store import write_json

FORMAT = 1

CURRENT: Run | None = None


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except ValueError:
        return default


class Run:
    r"""Status and progress record of a running job."""

    def __init__(self, path: str | Path, **info) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self.interval = _env_float("DAWGZ_PROGRESS_INTERVAL", 1.0)
        self.bars: dict[str, dict] = {}
        self.data: dict[str, Any] = {
            "format": FORMAT,
            "state": "RUNNING",
            "start": time.time(),
            "host": _hostname(),
            "pid": os.getpid(),
            **{k: v for k, v in info.items() if v is not None},
        }
        self.last = 0.0
        self.dirty = False
        self.ticker: threading.Thread | None = None
        self.closed = False
        self.write()

    def write(self) -> None:
        with self.lock:
            self.data["updated"] = time.time()
            self.data["progress"] = list(self.bars.values())
            try:
                write_json(self.path, self.data)
            except OSError:
                pass
            self.last = time.monotonic()
            self.dirty = False

    def touch(self, force: bool = False) -> None:
        with self.lock:
            self.dirty = True
            if force or time.monotonic() - self.last >= self.interval:
                self.write()
            elif self.ticker is None and not self.closed:
                self.ticker = threading.Thread(target=self._tick, daemon=True)
                self.ticker.start()

    def _tick(self) -> None:
        while not self.closed:
            time.sleep(self.interval)
            with self.lock:
                if self.dirty and not self.closed:
                    self.write()

    def bar(self, key: str, **fields) -> None:
        with self.lock:
            bar = self.bars.get(key)
            if bar is None:
                bar = self.bars[key] = {"desc": key}
            changed = any(bar.get(k) != v for k, v in fields.items())
            bar.update(fields)
            bar["t"] = time.time()
            if changed:
                done = bar.get("total") and bar.get("n", 0) >= bar["total"]
                self.touch(force=bool(done))

    def status(self, message: str) -> None:
        with self.lock:
            self.data["status"] = message
            self.touch(force=True)

    def finish(self, state: str, error: str | None = None) -> None:
        with self.lock:
            self.closed = True
            self.data["state"] = state
            self.data["end"] = time.time()
            if error:
                self.data["error"] = error
            self.write()


def _hostname() -> str:
    import socket

    return socket.gethostname()


# Log cooking

SI = {"": 1, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}

TQDM = re.compile(
    r"(?:(?P<desc>[^|\r\n]*?):\s*)?(?P<pct>\d{1,3})%\|[^|]*\|\s*"
    r"(?P<n>[\d.]+)(?P<ns>[kMGTP]?)(?P<unit1>[^\d\s/]*)/(?P<total>[\d.]+)(?P<ts>[kMGTP]?)\S*"
    r"(?:\s*\[(?P<info>[^\]]*)\])?"
)

TQDM_NOTOTAL = re.compile(
    r"^(?:(?P<desc>[^|\r\n]*?):\s*)?(?P<n>[\d.]+)(?P<ns>[kMGTP]?)(?P<unit>[a-zA-Z]+)\s*"
    r"\[(?P<info>\d[^\]]*/s[^\]]*)\]\s*$"
)

RATE = re.compile(r"([\d.]+)\s*([a-zA-Z]*)/s")
ETA = re.compile(r"<([\d:]+)")


def parse_bar(line: str) -> dict | None:
    r"""Parses a `tqdm` progress line into a progress entry."""

    match = TQDM.search(line)

    if match is not None:
        entry = {
            "desc": (match.group("desc") or "").strip() or "progress",
            "n": float(match.group("n")) * SI[match.group("ns")],
            "total": float(match.group("total")) * SI[match.group("ts")],
        }
    else:
        match = TQDM_NOTOTAL.search(line)
        if match is None:
            return None
        entry = {
            "desc": (match.group("desc") or "").strip() or "progress",
            "n": float(match.group("n")) * SI[match.group("ns")],
            "unit": match.group("unit"),
        }

    info = match.group("info") or ""
    parts = [p.strip() for p in info.split(",")]

    rate = RATE.search(info)
    if rate:
        entry["rate"] = float(rate.group(1))
        entry.setdefault("unit", rate.group(2) or "it")
    if "s/it" in info:
        m = re.search(r"([\d.]+)\s*s/it", info)
        if m and float(m.group(1)) > 0:
            entry["rate"] = 1 / float(m.group(1))

    eta = ETA.search(info)
    if eta:
        entry["eta"] = _clock(eta.group(1))

    postfix = [p for p in parts[2:] if p]
    if postfix:
        entry["postfix"] = ", ".join(postfix)

    for key in ("n", "total"):
        if key in entry and entry[key] == int(entry[key]):
            entry[key] = int(entry[key])

    return entry


def _clock(text: str) -> float | None:
    try:
        seconds = 0.0
        for p in text.split(":"):
            seconds = 60 * seconds + float(p)
        return seconds
    except ValueError:
        return None


class Cooker:
    r"""Collapses carriage-return redraws of a byte stream, like a terminal would.

    Regular lines are written through immediately. A line that is being redrawn with
    carriage returns is written at most once every `interval` seconds, and when it ends.
    """

    SPLIT = re.compile(rb"(\r\n|\r|\n)")

    def __init__(
        self,
        out_fd: int,
        tee_fd: int | None = None,
        run: Run | None = None,
        interval: float | None = None,
    ) -> None:
        self.out_fd = out_fd
        self.tee_fd = tee_fd
        self.run = run
        self.interval = _env_float("DAWGZ_LOG_INTERVAL", 10.0) if interval is None else interval
        self.line = bytearray()
        self.cursor = 0
        self.written = 0  # bytes of the current line already written
        self.redraw = False
        self.dirty = False
        self.last = 0.0

    def _write(self, data: bytes, fd: int | None = None) -> None:
        fd = self.out_fd if fd is None else fd
        view = memoryview(data)
        while view:
            try:
                n = os.write(fd, view)
            except InterruptedError:
                continue
            except OSError:
                return
            view = view[n:]

    def feed(self, data: bytes) -> None:
        if self.tee_fd is not None:
            self._write(data, self.tee_fd)

        for token in self.SPLIT.split(data):
            if not token:
                continue
            elif token in (b"\n", b"\r\n"):
                self._newline()
            elif token == b"\r":
                self.cursor = 0
                if not self.redraw:
                    self.redraw = True
            else:
                self._text(token)

        if self.redraw and self.dirty:
            self._progress()
            if time.monotonic() - self.last >= self.interval:
                self.checkpoint()
        elif not self.redraw and len(self.line) > self.written:
            self._write(bytes(self.line[self.written :]))
            self.written = len(self.line)

            # Bound memory for very long lines
            if len(self.line) > 65536:
                self.line.clear()
                self.cursor = 0
                self.written = 0

    def _text(self, token: bytes) -> None:
        end = self.cursor + len(token)
        self.line[self.cursor : end] = token
        self.cursor = end
        self.dirty = True

    def _newline(self) -> None:
        if self.redraw:
            self._progress()
            self._write(
                b"\r" + bytes(self.line) + b"\n" if self.written else bytes(self.line) + b"\n"
            )
        else:
            self._write(bytes(self.line[self.written :]) + b"\n")

        self.line.clear()
        self.cursor = 0
        self.written = 0
        self.redraw = False
        self.dirty = False

    def checkpoint(self) -> None:
        r"""Writes the current state of a redrawn line, to be overwritten later."""

        if self.redraw and self.dirty:
            prefix = b"\r" if self.written else b""
            self._write(prefix + bytes(self.line))
            self.written = max(len(self.line), 1)
            self.dirty = False
            self.last = time.monotonic()

    def _progress(self) -> None:
        if self.run is None:
            return

        try:
            text = self.line.decode(errors="replace")
        except Exception:  # pragma: no cover
            return

        entry = parse_bar(text)

        if entry is not None:
            desc = entry.pop("desc")
            self.run.bar(desc, **entry)

    def close(self) -> None:
        if self.redraw:
            if self.dirty or not self.written:
                self.checkpoint()
            self._write(b"\n")
        elif len(self.line) > self.written:
            self._write(bytes(self.line[self.written :]))


@contextmanager
def capture(
    out_fd: int | None = None, tee: bool = False, run: Run | None = None
) -> Iterator[Cooker]:
    r"""Redirects the standard streams through a `Cooker`.

    Arguments:
        out_fd: The file descriptor of the log file. If `None`, use the original standard
            output (e.g. the Slurm output file).
        tee: Whether to also copy the raw streams to the original standard output.
        run: The run record to report parsed progress to.
    """

    import select

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, ValueError, OSError):
            pass

    saved = os.dup(1), os.dup(2)
    r, w = os.pipe()
    os.dup2(w, 1)
    os.dup2(w, 2)
    os.close(w)

    cooker = Cooker(
        saved[0] if out_fd is None else out_fd,
        tee_fd=saved[0] if tee else None,
        run=run,
    )

    def pump() -> None:
        while True:
            try:
                ready, _, _ = select.select([r], [], [], cooker.interval)
            except (OSError, ValueError):
                break

            if not ready:
                cooker.checkpoint()
                continue

            try:
                data = os.read(r, 65536)
            except OSError:
                break

            if not data:
                break

            cooker.feed(data)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()

    # Python streams may not write to the standard file descriptors (e.g. under pytest)
    streams = sys.stdout, sys.stderr
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout = open(
        1, "w", buffering=1, encoding=encoding, errors="backslashreplace", closefd=False
    )
    sys.stderr = open(
        2, "w", buffering=1, encoding=encoding, errors="backslashreplace", closefd=False
    )

    try:
        yield cooker
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, ValueError, OSError):
                pass

        sys.stdout, sys.stderr = streams

        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)

        # Subprocesses that outlive the job may keep the pipe open
        thread.join(timeout=5.0)
        cooker.close()

        os.close(saved[0])
        os.close(saved[1])

        if not thread.is_alive():
            os.close(r)


# Entry points


def trace(error: BaseException) -> str:
    import traceback

    lines = traceback.format_exception(type(error), error, error.__traceback__)
    lines = [line for line in lines if "dawgz/runtime.py" not in line]

    return "".join(lines).rstrip("\n")


def load(path: Path, tag: str, i: int | None) -> bytes:
    from .utils import BYTES_HEADER, bytes_load

    with open(path / f"{tag}.pkl", "rb") as f:
        if f.read(len(BYTES_HEADER)) == BYTES_HEADER:
            return bytes(bytes_load(f, i or 0))
        f.seek(0)
        return f.read()


def execute(
    data: bytes,
    run: Run | None,
    out_fd: int | None = None,
    tee: bool = False,
    capture_streams: bool = True,
) -> int:
    r"""Runs a pickled callable, reporting its status. Returns the exit code."""

    import pickle

    global CURRENT
    CURRENT = run

    if run is not None:
        os.environ["DAWGZ_RUN_FILE"] = str(run.path)

    raw = os.environ.get("DAWGZ_RAW_LOGS", "") not in ("", "0")
    code, error = 0, None

    @contextmanager
    def nothing() -> Iterator[None]:
        yield None

    if raw or not capture_streams:
        context = nothing()
    else:
        context = capture(out_fd, tee=tee, run=run)

    with context:
        try:
            pickle.loads(data)()
        except SystemExit as e:
            if e.code is None or isinstance(e.code, int):
                code = e.code or 0
            else:
                code = 1
                print(e.code, file=sys.stderr, flush=True)
            if code:
                error = f"SystemExit: {e.code}"
        except BaseException as e:
            code = 130 if isinstance(e, KeyboardInterrupt) else 1
            error = f"{type(e).__name__}: {e}"
            text = trace(e)
            if run is not None:
                run.data["trace"] = text[-20000:]
            print(text, file=sys.stderr, flush=True)

    if run is not None:
        run.finish("COMPLETED" if code == 0 else "FAILED", error)

    CURRENT = None

    return code


def main(path: str, *argv: str) -> None:
    r"""Entry point of Slurm jobs (see the generated `run.py`).

    Arguments:
        path: The workflow directory.
        argv: Either the tag of the job, or `--pack` and the name of a pack, whose
            jobs are the tasks of a Slurm array.
    """

    from .store import log_file, read_json, run_file

    path = Path(path)
    index = os.environ.get("SLURM_ARRAY_TASK_ID")
    task = int(index) if index not in (None, "") else None
    jobid = os.environ.get("SLURM_JOB_ID")

    if argv[0] == "--pack":
        pack = argv[1]
        tag = read_json(path / f"{pack}.json")[task]
        source, i, out_fd = pack, task, None
        arrayid = os.environ.get("SLURM_ARRAY_JOB_ID")
        jobid = f"{arrayid}_{task}" if arrayid else jobid
        logfile, runfile = log_file(path, tag), run_file(path, tag)
    else:
        tag = argv[0]
        source, i, out_fd = tag, task, None
        logfile, runfile = None, run_file(path, tag, task)
        if task is not None:
            jobid = os.environ.get("SLURM_ARRAY_JOB_ID") or jobid

    # Only the first task/rank reports the status of the job
    leader = os.environ.get("SLURM_PROCID", "0") == "0" and os.environ.get("RANK", "0") == "0"
    run = Run(runfile, jobid=jobid) if leader else None

    if logfile is not None:
        out_fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        if os.environ.get("DAWGZ_RAW_LOGS", "") not in ("", "0"):
            os.dup2(out_fd, 1)
            os.dup2(out_fd, 2)

    try:
        data = load(path, source, i)
    except BaseException as e:
        text = trace(e)
        if out_fd is not None:
            os.write(out_fd, (text + "\n").encode())
        print(text, file=sys.stderr, flush=True)
        if run is not None:
            run.finish("FAILED", f"{type(e).__name__}: {e}")
        sys.exit(1)

    sys.exit(execute(data, run, out_fd=out_fd))


def runner(paths: list[str]) -> str:
    r"""Returns the code of the `run.py` script of a Slurm workflow."""

    return RUNNER.replace("__PATHS__", repr(list(paths)))


RUNNER = """#!/usr/bin/env python
# Generated by dawgz. Runs the job whose tag is given as first argument.

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Make the modules of the submission directory importable, as when the script ran
for path in reversed(__PATHS__):
    if path not in sys.path:
        sys.path.insert(1, path)

try:
    from dawgz.runtime import main
except ImportError:  # dawgz is not available in the job environment
    main = None

if main is not None:
    main(HERE, *sys.argv[1:])
else:
    import json
    import pickle
    import struct

    name = sys.argv[-1]

    with open(os.path.join(HERE, name + ".pkl"), "rb") as f:
        data = f.read()

    if data.startswith(b"BYTES_LIST"):
        i = int(os.environ["SLURM_ARRAY_TASK_ID"])
        (n,) = struct.unpack_from("<Q", data, 10)
        sizes = struct.unpack_from(f"<{n}Q", data, 18)
        offset = 18 + 8 * n + sum(sizes[:i])
        data = data[offset : offset + sizes[i]]

    if sys.argv[1] == "--pack":
        with open(os.path.join(HERE, name + ".json")) as f:
            tag = json.load(f)[i]
        fd = os.open(os.path.join(HERE, tag + ".log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        os.dup2(fd, 1)
        os.dup2(fd, 2)

    pickle.loads(data)()
"""
