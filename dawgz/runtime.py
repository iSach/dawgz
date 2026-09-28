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

    MAX_BARS = 8

    def bar(self, key: str, **fields) -> None:
        r"""Updates the progress bar `key` (e.g. a line of the output, or a `Progress`)."""

        with self.lock:
            bar = self.bars.get(key)
            if bar is None:
                bar = self.bars[key] = {"desc": key}
                # Keep the most recently updated bars only
                while len(self.bars) > self.MAX_BARS:
                    oldest = min(self.bars, key=lambda k: self.bars[k].get("t", 0))
                    del self.bars[oldest]
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

    try:
        return _parse_bar(line)
    except (ValueError, OverflowError, ZeroDivisionError, KeyError):
        return None


def _parse_bar(line: str) -> dict | None:
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

    if entry.get("total") == 0:
        del entry["total"]

    return entry


def _clock(text: str) -> float | None:
    try:
        seconds = 0.0
        for p in text.split(":"):
            seconds = 60 * seconds + float(p)
        return seconds
    except ValueError:
        return None


class Row:
    __slots__ = ("buf", "written", "redraw", "dirty")

    def __init__(self) -> None:
        self.buf = ""
        self.written = 0  # characters already written to the log
        self.redraw = False  # the row has been redrawn with a carriage return
        self.dirty = False


class Cooker:
    r"""Renders a stream like a terminal would, and writes it compactly.

    Regular lines are written through immediately. Lines redrawn in place, with carriage
    returns or cursor movements (e.g. nested `tqdm` bars), are written at most once every
    `interval` seconds, and when they scroll out of reach.
    """

    SPLIT = re.compile(r"(\r\n|\r|\n|\x1b\[\d*A)")
    MAX_LINE = 1 << 16

    def __init__(
        self,
        out_fd: int,
        tee_fd: int | None = None,
        run: Run | None = None,
        interval: float | None = None,
    ) -> None:
        import codecs

        self.out_fd = out_fd
        self.tee_fd = tee_fd
        self.run = run
        self.interval = _env_float("DAWGZ_LOG_INTERVAL", 10.0) if interval is None else interval
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.rows = [Row()]
        self.r = 0  # cursor row (within `rows`)
        self.c = 0  # cursor column
        self.up = 0  # largest cursor movement upwards, rows within reach are kept
        self.shown = 0  # length of the last snapshot of a multi-row screen
        self.last = 0.0
        self.closed = False

    def _write(self, data: str | bytes, fd: int | None = None) -> None:
        fd = self.out_fd if fd is None else fd
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
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

        for token in self.SPLIT.split(self.decoder.decode(data)):
            if not token:
                continue
            elif token in ("\n", "\r\n"):
                self._newline()
            elif token == "\r":
                self.c = 0
                self.rows[self.r].redraw = True
            elif token.startswith("\x1b["):
                n = int(token[2:-1] or "1")
                self.up = max(self.up, n)
                self.r = max(self.r - n, 0)
                self.rows[self.r].redraw = True
            else:
                self._text(token)

        self._flush()

    def _text(self, token: str) -> None:
        row = self.rows[self.r]
        end = min(self.c + len(token), self.MAX_LINE)
        if self.c < end:
            buf = row.buf
            if len(buf) < self.c:
                buf += " " * (self.c - len(buf))
            row.buf = buf[: self.c] + token[: end - self.c] + buf[end:]
        self.c = end
        row.dirty = True

    def _newline(self) -> None:
        self.r += 1
        self.c = 0
        if self.r == len(self.rows):
            self.rows.append(Row())

        # Rows out of reach of the cursor are final. Redrawn rows are kept one more line,
        # as programs drawing several bars (e.g. nested tqdm) move up only afterwards.
        while len(self.rows) > 1 and self.r > max(self.up, int(self.rows[0].redraw)):
            self._commit(self.rows.pop(0))
            self.r -= 1

    def _commit(self, row: Row) -> None:
        self._progress(row, 0)

        if row.redraw:
            buf = row.buf.rstrip(" ")  # e.g. bars cleared with spaces
            pad = " " * max(self.shown - len(buf), 0) if self.shown and row.written else ""
            prefix = "\r" if row.written else ""
            self._write(prefix + buf + pad + "\n")
        else:
            self._write(row.buf[row.written :] + "\n")

        self.shown = 0

    def _flush(self) -> None:
        if self.up == 0 and len(self.rows) == 1:
            row = self.rows[0]
            if row.redraw:
                if row.dirty:
                    self._progress(row, 0)
                    if time.monotonic() - self.last >= self.interval:
                        self.checkpoint()
            elif len(row.buf) > row.written:
                # Plain output is written through
                self._write(row.buf[row.written :])
                row.written = len(row.buf)
                if len(row.buf) >= self.MAX_LINE:  # bound memory for very long lines
                    row.buf = ""
                    row.written = 0
                    self.c = 0
        else:
            dirty = False
            for k, row in enumerate(self.rows):
                if row.dirty:
                    self._progress(row, k)
                    dirty = True
            if dirty and time.monotonic() - self.last >= self.interval:
                self.checkpoint()

    def checkpoint(self) -> None:
        r"""Writes the current state of redrawn rows, to be overwritten later."""

        rows = [row for row in self.rows if row.buf.strip()]
        if not rows or not any(row.dirty for row in self.rows):
            return

        base = self.rows[0]

        if len(self.rows) == 1:
            if not base.redraw:
                return
            prefix = "\r" if base.written else ""
            self._write(prefix + base.buf)
            base.written = max(len(base.buf), 1)
        else:
            # A snapshot of the screen on the first line, e.g. "epoch 3/10 │ 45% batches"
            snapshot = " │ ".join(row.buf.strip() for row in rows)
            snapshot += " " * max(self.shown - len(snapshot), 0)
            prefix = "\r" if base.written else ""
            self._write(prefix + snapshot)
            base.written = max(len(snapshot), 1)
            base.redraw = True
            self.shown = len(snapshot)

        for row in self.rows:
            row.dirty = False
        self.last = time.monotonic()

    def _progress(self, row: Row, k: int) -> None:
        if self.run is None:
            return

        try:
            entry = parse_bar(row.buf)
            if entry is not None:
                self.run.bar(f"line{k}", **entry)
        except Exception:  # never let parsing break the stream
            pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True

        tail = self.decoder.decode(b"", final=True)
        if tail:
            self._text(tail)

        rows = self.rows
        while rows and not rows[-1].buf and not rows[-1].written:
            rows.pop()

        for k, row in enumerate(rows):
            last = k == len(rows) - 1
            if last and not row.redraw and len(rows) == 1:
                self._progress(row, k)
                self._write(row.buf[row.written :])  # keep a missing final newline
            else:
                self._commit(row)


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

    # The cooker owns its descriptors: they stay valid if the thread outlives the job
    out = os.dup(saved[0] if out_fd is None else out_fd)
    tee_fd = os.dup(saved[0]) if tee else None
    cooker = Cooker(out, tee_fd=tee_fd, run=run)

    def pump() -> None:
        broken = False
        while True:
            try:
                ready, _, _ = select.select([r], [], [], cooker.interval)
                if not ready:
                    if not broken:
                        cooker.checkpoint()
                    continue
                data = os.read(r, 65536)
            except (OSError, ValueError):
                break

            if not data:
                break

            try:
                if broken:
                    cooker._write(data)
                else:
                    cooker.feed(data)
            except Exception:
                # Never stop draining the pipe, which would block the job
                broken = True
                cooker._write(data)

        try:
            cooker.close()
        except Exception:
            pass

        os.close(r)
        os.close(out)
        if tee_fd is not None:
            os.close(tee_fd)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()

    # Python streams may not write to the standard file descriptors (e.g. under pytest)
    streams = sys.stdout, sys.stderr
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout = open(
        1, "w", buffering=1, encoding=encoding, errors="backslashreplace", closefd=False
    )  # noqa: SIM115
    sys.stderr = open(
        2, "w", buffering=1, encoding=encoding, errors="backslashreplace", closefd=False
    )  # noqa: SIM115

    try:
        yield cooker
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, ValueError, OSError):
                pass

        sys.stdout, sys.stderr = streams

        # Closes the last write ends of the pipe held by this process
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])

        # Processes that outlive the job may keep the pipe open: their output keeps
        # going to the log, from a daemon thread
        thread.join(timeout=1.0)


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
    path: str | Path | None = None,
) -> int:
    r"""Runs a pickled callable, reporting its status. Returns the exit code."""

    from .payload import resolve

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
            resolve(data, path)()
        except SystemExit as e:
            if e.code is None or isinstance(e.code, int):
                code = e.code or 0
                if not 0 <= code <= 255:
                    code = 1
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

    sys.exit(execute(data, run, out_fd=out_fd, path=path))


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

# A snapshot of the local code, taken at submission, comes first
if os.path.isdir(os.path.join(HERE, "snapshot")):
    sys.path.insert(0, os.path.join(HERE, "snapshot"))

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

    obj = pickle.loads(data)

    if isinstance(obj, tuple) and obj[:1] in (("dawgz-call-v1",), ("dawgz-ref-v1",)):
        kind, fun, args = obj
        if kind == "dawgz-ref-v1":
            with open(os.path.join(HERE, "fn_" + fun + ".pkl"), "rb") as f:
                fun = f.read()
        args, kwargs = pickle.loads(args)
        pickle.loads(fun)(*args, **kwargs)
    else:
        obj()
"""
