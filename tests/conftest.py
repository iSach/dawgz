"""Shared fixtures. Every test runs against the fake Slurm in `tools/fakeslurm`."""

import json
import os
import pytest
import shutil
import sys
import time

from collections.abc import Callable
from pathlib import Path

import dawgz

FAKESLURM = Path(__file__).resolve().parents[1] / "tools" / "fakeslurm"


@pytest.fixture(autouse=True)
def fake_slurm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r"""Isolates tests from the real cluster and from the user's files."""

    state = tmp_path / "fakeslurm"
    state.mkdir()

    # Jobs run with the interpreter of the tests
    python = os.path.dirname(sys.executable)
    monkeypatch.setenv("PATH", f"{FAKESLURM}:{python}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKESLURM_DIR", str(state))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("FAKESLURM_EXEC", raising=False)
    monkeypatch.delenv("DAWGZ_BACKEND", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")

    for command in ("sbatch", "sacct", "scancel", "squeue", "srun"):
        assert shutil.which(command) == str(FAKESLURM / command), f"{command} is not faked"

    dawgz.set_dawgz_dir(tmp_path / ".dawgz")

    return state


@pytest.fixture()
def slurm_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    r"""Makes the fake Slurm execute submitted jobs."""

    monkeypatch.setenv("FAKESLURM_EXEC", "1")


@pytest.fixture()
def wait_slurm(fake_slurm: Path) -> Callable[[float], None]:
    r"""Waits until all fake Slurm jobs are terminal (reads fake state files only)."""

    terminal = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}

    def wait(timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            jobs = [json.loads(f.read_text()) for f in (fake_slurm / "jobs").glob("*.json")]
            states = [t for j in jobs for t in j["tasks"].values()]
            if all(
                t["state"] in terminal or "DependencyNever" in t.get("reason", "") for t in states
            ):
                return
            time.sleep(0.05)
        raise TimeoutError("fake Slurm jobs did not finish")

    return wait


def sacct_calls(fake_slurm: Path) -> int:
    calls = fake_slurm / "calls"
    return (
        sum(line.startswith("sacct") for line in calls.read_text().splitlines())
        if calls.exists()
        else 0
    )
