r"""Snapshots of the local code imported by workflows.

Functions defined in the main script are pickled by value when jobs are created, but
the modules they import are pickled by reference: Slurm jobs import them when they
start, possibly hours later, from files that may have been edited meanwhile. A
snapshot copies the local code (modules and packages that are not installed in the
environment) to the workflow directory, where jobs import it from instead.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
import warnings

from pathlib import Path

MAX_FILE = 1 << 20  # larger files (e.g. data) are not copied
MAX_TOTAL = 256 << 20
SKIP_DIRS = {"__pycache__", ".git", ".hg", ".svn", ".dawgz", "node_modules", ".venv", "venv"}


def installed() -> list[str]:
    r"""Directories of installed packages and of the standard library."""

    paths = set()

    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        path = sysconfig.get_paths().get(key)
        if path:
            paths.add(os.path.realpath(path))

    for prefix in {sys.prefix, sys.base_prefix, sys.exec_prefix}:
        paths.add(os.path.realpath(prefix))

    return sorted(paths)


def inside(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def local_sources(script_dir: str | None = None) -> dict[str, str]:
    r"""Finds the local top-level modules and packages to snapshot.

    Returns:
        A mapping from top-level name to its file or directory.
    """

    system = installed()
    sources: dict[str, str] = {}

    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if not file or name == "__main__" or "." in name and name.split(".")[0] in sources:
            continue

        file = os.path.realpath(file)
        top = name.split(".")[0]

        if top in ("dawgz",) or inside(file, system) or not file.endswith(".py"):
            continue

        # Directory from which the module is imported, e.g. `/src` for `/src/a/b/c.py`
        parts = name.split(".")
        depth = len(parts) if file.endswith("__init__.py") else len(parts) - 1
        try:
            root = Path(file).parents[depth]
        except IndexError:
            continue

        candidate = root / top
        if candidate.is_dir():
            sources.setdefault(top, str(candidate))
        elif (root / f"{top}.py").is_file():
            sources.setdefault(top, str(root / f"{top}.py"))

    # Sibling modules and packages of the script, even if not imported yet
    if script_dir and not inside(os.path.realpath(script_dir), system):
        for entry in os.scandir(script_dir):
            name = entry.name
            if entry.is_file() and name.endswith(".py"):
                sources.setdefault(name[:-3], entry.path)
            elif (
                entry.is_dir()
                and name not in SKIP_DIRS
                and os.path.exists(os.path.join(entry.path, "__init__.py"))
            ):
                sources.setdefault(name, entry.path)

    sources.pop("dawgz", None)

    return sources


def take(destination: Path, script_dir: str | None = None) -> dict:
    r"""Copies the local code to `destination`. Returns a summary."""

    destination.mkdir(parents=True, exist_ok=True)
    files, total, skipped = 0, 0, 0

    for name, source in sorted(local_sources(script_dir).items()):
        if os.path.isfile(source):
            shutil.copy2(source, destination / f"{name}.py")
            files += 1
            total += os.path.getsize(source)
            continue

        for folder, dirs, names in os.walk(source):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            target = destination / name / os.path.relpath(folder, source)
            target.mkdir(parents=True, exist_ok=True)

            for file in names:
                path = os.path.join(folder, file)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if file.endswith((".pyc", ".pyo")) or size > MAX_FILE or total + size > MAX_TOTAL:
                    skipped += 1
                    continue
                shutil.copy2(path, target / file)
                files += 1
                total += size

    if total >= MAX_TOTAL:
        warnings.warn(
            f"the code snapshot was truncated to {MAX_TOTAL >> 20} MB",
            RuntimeWarning,
            stacklevel=3,
        )

    return {"files": files, "bytes": total, "skipped": skipped}
