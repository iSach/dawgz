"""Contract tests between the Python records and the Rust TUI (skipped if not built)."""

import pytest
import re
import subprocess

from collections.abc import Callable

import dawgz

from dawgz.cli import find_tui

TUI = find_tui()

pytestmark = pytest.mark.skipif(TUI is None, reason="dawgz-tui is not built")

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dawgz.job
def step(i: int) -> None:
    for _ in dawgz.progress(range(3), desc="step"):
        pass
    if i == 3:
        raise ValueError("bad step")


@dawgz.job
def merge() -> None:
    print("merged")


def snapshot(*args: str) -> str:
    out = subprocess.run(
        [TUI, "--snapshot", "160x48", "--dir", str(dawgz.get_dawgz_dir()), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return ANSI.sub("", out)


def test_tui_reads_python_records(slurm_exec: None, wait_slurm: Callable) -> None:
    steps = step.map(range(6))
    final = merge().after(*steps, status="any")
    array = dawgz.array(*step.map(range(3)))
    dawgz.schedule(final, array, backend="slurm", name="contract.py", quiet=True)

    wait_slurm()

    text = snapshot()
    assert "contract.py" in text
    assert "step ×6" in text
    assert "step[0-2]" in text
    assert "merge" in text

    # Same totals as the Python view
    view = dawgz.store.Workflow.open(
        next(p for p in dawgz.get_dawgz_dir().iterdir() if p.is_dir())
    )
    totals = view.totals()
    assert totals["failed"] == 1
    assert f"✔{totals['done']}" in text.replace(" ", "")

    text = snapshot("--row", "0", "--expand")
    assert "step(3)" in text

    text = snapshot("--tab", "graph")
    assert "merge" in text


def test_tui_local_workflow() -> None:
    dawgz.schedule(merge().after(*step.map([0, 1])), backend="local", name="local.py", quiet=True)

    text = snapshot("--offline", "--tab", "logs", "--row", "2")
    assert "local.py" in text
    assert "merged" in text
