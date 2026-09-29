r"""Minimal terminal rendering (styles, bars and tables) without third-party packages."""

from __future__ import annotations

import os
import re
import shutil
import sys
import time

from collections.abc import Sequence

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

CODES = {
    "bold": "1",
    "dim": "2",
    "italic": "3",
    "underline": "4",
    "reverse": "7",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "gray": "90",
    "bright_red": "91",
    "bright_green": "92",
    "bright_yellow": "93",
    "bright_blue": "94",
    "bright_magenta": "95",
    "bright_cyan": "96",
}

# Category -> (glyph, label, color)
STATES = {
    "done": ("✔", "done", "green"),
    "running": ("●", "running", "cyan"),
    "pending": ("◌", "pending", "gray"),
    "failed": ("✘", "failed", "red"),
    "cancelled": ("⊘", "cancelled", "yellow"),
    "unknown": ("?", "unknown", "magenta"),
}

ORDER = ("done", "running", "failed", "cancelled", "pending", "unknown")


def supports_color(stream: object = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
        return True

    stream = stream or sys.stdout

    try:
        return stream.isatty() and os.environ.get("TERM") != "dumb"
    except (AttributeError, ValueError):
        return False


COLOR = supports_color()


def set_color(enabled: bool) -> None:
    global COLOR
    COLOR = enabled


def style(text: str, *names: str) -> str:
    if not COLOR or not names or not text:
        return text

    codes = ";".join(CODES[n] for n in names if n in CODES)
    return f"\x1b[{codes}m{text}\x1b[0m"


def strip(text: str) -> str:
    return ANSI.sub("", text)


def _char_width(c: str) -> int:
    if c < "ᄀ":
        return 1

    import unicodedata

    if unicodedata.combining(c):
        return 0

    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def width(text: str) -> int:
    text = strip(text)

    if text.isascii():
        return len(text)

    return sum(map(_char_width, text))


def truncate(text: str, size: int) -> str:
    r"""Truncates (possibly styled) text to a visible width, appending an ellipsis."""

    if width(text) <= size:
        return text
    if size <= 0:
        return ""

    out, w, i = [], 0, 0

    while i < len(text):
        match = ANSI.match(text, i)

        if match:
            out.append(match.group())
            i = match.end()
            continue

        cw = _char_width(text[i])
        if w + cw > size - 1:
            break

        out.append(text[i])
        w += cw
        i += 1

    out.append("…")

    if COLOR and "\x1b[" in text:
        out.append("\x1b[0m")

    return "".join(out)


def pad(text: str, size: int, align: str = "<") -> str:
    gap = max(size - width(text), 0)

    if align == ">":
        return " " * gap + text
    elif align == "^":
        return " " * (gap // 2) + text + " " * (gap - gap // 2)
    else:
        return text + " " * gap


def terminal_width(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns


# Widgets


def glyph(cat: str) -> str:
    g, _, color = STATES.get(cat, STATES["unknown"])
    return style(g, color)


def label(cat: str, text: str | None = None) -> str:
    g, name, color = STATES.get(cat, STATES["unknown"])
    return style(f"{g} {text or name}", color)


def bar(segments: Sequence[tuple[float, str]], size: int) -> str:
    r"""Renders a stacked progress bar.

    Arguments:
        segments: A sequence of (fraction, color) pairs, whose fractions sum to at most 1.
        size: The width of the bar in cells.
    """

    if size <= 0:
        return ""

    out = []
    used = 0
    acc = 0.0
    last = None

    for fraction, color in segments:
        acc += max(fraction, 0.0)
        n = min(int(acc * size), size) - used
        if n > 0:
            out.append(style("━" * n, color))
            used += n
        if fraction > 0:
            last = color

    # Half cell for the remainder
    if used < size and last is not None and min(acc, 1.0) * size - used >= 0.5:
        out.append(style("╸", last))
        used += 1

    if used < size:
        out.append(style("━" * (size - used), "gray", "dim") if COLOR else "─" * (size - used))

    return "".join(out)


def state_bar(counts: dict[str, int], size: int, fraction: float | None = None) -> str:
    r"""Renders a bar whose segments are colored by state category."""

    total = sum(counts.values()) or 1
    segments = [(counts.get(c, 0) / total, STATES[c][2]) for c in ORDER if c != "pending"]

    if fraction is not None:
        # Account for the partial progress of running elements
        finished = sum(counts.get(c, 0) for c in ("done", "failed", "cancelled")) / total
        segments = [(f, c) for f, c in segments if c != "cyan"]
        segments.insert(1, (max(fraction - finished, 0.0), "cyan"))

    return bar(segments, size)


def table(
    rows: Sequence[Sequence[str]],
    header: Sequence[str] | None = None,
    align: str | None = None,
    flex: int | None = None,
    max_width: int | None = None,
    gap: int = 2,
    indent: int = 0,
) -> list[str]:
    r"""Lays out rows of (possibly styled) cells into aligned lines.

    Arguments:
        align: One alignment character (`<`, `>` or `^`) per column.
        flex: The column that shrinks when the table exceeds `max_width`.
    """

    rows = [list(r) for r in rows]
    ncols = max(map(len, rows + ([header] if header else [[]])), default=0)

    for r in rows:
        r.extend([""] * (ncols - len(r)))

    align = (align or "<" * ncols).ljust(ncols, "<")
    widths = [0] * ncols

    for r in rows + ([list(header)] if header else []):
        for j, cell in enumerate(r):
            widths[j] = max(widths[j], width(cell))

    # Drop empty columns
    keep = [j for j in range(ncols) if widths[j] > 0]

    if max_width is not None and flex is not None and flex in keep:
        total = indent + sum(widths[j] for j in keep) + gap * (len(keep) - 1)
        if total > max_width:
            widths[flex] = max(widths[flex] - (total - max_width), 8)

    lines = []
    spacer = " " * gap

    if header:
        cells = [
            pad(style(truncate(header[j], widths[j]), "bold"), widths[j], align[j]) for j in keep
        ]
        lines.append(" " * indent + spacer.join(cells).rstrip())

    for r in rows:
        cells = [pad(truncate(r[j], widths[j]), widths[j], align[j]) for j in keep]
        lines.append(" " * indent + spacer.join(cells).rstrip())

    return lines


# Formatting


def duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""

    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)

    if d:
        return f"{d}d{h:02d}h"
    elif h:
        return f"{h}:{m:02d}:{s:02d}"
    else:
        return f"{m}:{s:02d}"


def age(timestamp: float | None, now: float | None = None) -> str:
    if not timestamp:
        return ""

    delta = (now or time.time()) - timestamp

    if delta < 60:
        return f"{max(int(delta), 0)}s ago"
    elif delta < 3600:
        return f"{int(delta // 60)}m ago"
    elif delta < 86400:
        return f"{int(delta // 3600)}h ago"
    else:
        return f"{int(delta // 86400)}d ago"


def size(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def count(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e4:
        return f"{n / 1e3:.0f}k"
    if n != int(n):
        return f"{n:.1f}"
    return str(int(n))


# Syntax highlighting

KEYWORDS = frozenset(
    "False None True and as assert async await break class continue def del elif else "
    "except finally for from global if import in is lambda nonlocal not or pass raise "
    "return try while with yield match case".split()
)


def highlight_python(source: str) -> str:
    r"""Highlights Python source code with ANSI colors."""

    if not COLOR:
        return source

    import io
    import tokenize

    lines = source.splitlines(keepends=True)
    out = []
    prev = (1, 0)

    def text(a: tuple[int, int], b: tuple[int, int]) -> str:
        if a[0] == b[0]:
            return lines[a[0] - 1][a[1] : b[1]]
        chunk = [lines[a[0] - 1][a[1] :]]
        chunk.extend(lines[a[0] : b[0] - 1])
        chunk.append(lines[b[0] - 1][: b[1]])
        return "".join(chunk)

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return source

    decorator = False

    for tok in tokens:
        if tok.start[0] > len(lines):
            break
        out.append(text(prev, tok.start))
        s = tok.string
        if tok.type == tokenize.COMMENT:
            s = style(s, "gray", "italic")
        elif tok.type == tokenize.STRING or tok.type == getattr(tokenize, "FSTRING_START", -1):
            s = style(s, "green")
        elif tok.type == tokenize.NUMBER:
            s = style(s, "magenta")
        elif tok.type == tokenize.NAME and s in KEYWORDS:
            s = style(s, "blue", "bold")
        elif tok.type == tokenize.OP and s == "@":
            decorator = True
            s = style(s, "yellow")
        elif tok.type == tokenize.NAME and decorator:
            s = style(s, "yellow")
        elif tok.type in (tokenize.NEWLINE, tokenize.NL):
            decorator = False
        out.append(s)
        prev = tok.end

    return "".join(out)


def highlight_shell(source: str) -> str:
    if not COLOR:
        return source

    out = []
    for line in source.splitlines():
        if line.startswith("#SBATCH"):
            key, _, value = line[8:].partition("=")
            out.append(
                style("#SBATCH ", "gray")
                + style(key, "cyan")
                + ("=" + style(value, "yellow") if value else "")
            )
        elif line.startswith("#"):
            out.append(style(line, "gray"))
        else:
            out.append(line)
    return "\n".join(out)


def highlight_logs(text: str) -> str:
    if not COLOR:
        return text

    out = []
    for line in text.splitlines():
        lower = line.lower()
        if "traceback" in lower or "error" in lower or "exception" in lower:
            out.append(style(line, "red"))
        elif "warn" in lower:
            out.append(style(line, "yellow"))
        else:
            out.append(line)
    return "\n".join(out)
