#!/usr/bin/env python
r"""Runs demo workflows on a fake Slurm cluster, to try the CLI and TUI safely.

    python tools/demo.py [DIR]

Everything happens in DIR (default: a temporary directory): the fake Slurm state, the
workflows and their logs. The fake `sbatch` executes jobs locally in the background.
The script prints the environment to use for `dawgz` and `dawgz-tui` afterwards.
"""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FAKESLURM = REPO / "tools" / "fakeslurm"

SWEEP = """
import random
import time

import dawgz


@dawgz.job(cpus=2, time="10:00")
def fit(lr: float, seed: int) -> None:
    random.seed(seed)
    steps = random.randint(40, 90)
    for step in dawgz.progress(range(steps), desc="fit"):
        time.sleep(0.05 + 0.1 * random.random())
    print(f"lr={lr} seed={seed} loss={random.random():.4f}")


@dawgz.job(cpus=1, time="5:00")
def select() -> None:
    print("best: lr=0.001")


if __name__ == "__main__":
    fits = [fit(lr, seed) for lr in (1e-2, 1e-3, 1e-4) for seed in range(20)]
    dawgz.schedule(select().after(*fits), name="sweep.py")
"""

TIMEOUT = """
import time

import dawgz


@dawgz.job(time="0:04")
def slow() -> None:
    for _ in dawgz.progress(range(100), desc="slow"):
        time.sleep(0.2)


@dawgz.job
def cleanup() -> None:
    print("cleaning up")


if __name__ == "__main__":
    job = slow()
    dawgz.schedule(cleanup().after(job, status="any"), job, name="timeout.py")
"""

LOCAL = """
import time

import dawgz


@dawgz.job
def download() -> None:
    for _ in dawgz.progress(range(10), desc="download", unit="MB"):
        time.sleep(0.02)


@dawgz.job
def tokenize(shard: int) -> None:
    time.sleep(0.05)
    print(f"shard {shard}: 12345 tokens")


@dawgz.job
def stats() -> None:
    print("vocabulary: 50257")


if __name__ == "__main__":
    shards = tokenize.map(range(6))
    dl = download()
    shards = [s.after(dl) for s in shards]
    dawgz.schedule(stats().after(*shards), name="prepare.py", backend="local", workers=3, quiet=True)
"""


def fish_quote(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def main() -> None:
    root = Path(
        sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="dawgz-demo-")
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "slurm").mkdir(exist_ok=True)

    python = Path(sys.executable).parent
    env = {
        **os.environ,
        "PATH": f"{FAKESLURM}:{python}:{os.environ['PATH']}",
        "FAKESLURM_DIR": str(root / "slurm"),
        "FAKESLURM_EXEC": "1",
        "FAKESLURM_CPUS": "8",
        "DAWGZ_DIR": str(root / ".dawgz"),
        "DAWGZ_BACKEND": "slurm",
        "DAWGZ_NO_GLOBAL_REGISTRY": "1",
    }

    # Never reach a real cluster
    for command in ("sbatch", "sacct", "scancel", "srun"):
        assert shutil.which(command, path=env["PATH"]) == str(FAKESLURM / command)

    for name, code in (("prepare.py", LOCAL), ("sweep.py", SWEEP), ("timeout.py", TIMEOUT)):
        (root / name).write_text(textwrap.dedent(code))

    (root / "showcase.py").write_text((REPO / "examples" / "showcase.py").read_text())

    for script in ("prepare.py", "timeout.py", "sweep.py", "showcase.py"):
        subprocess.run([sys.executable, script], cwd=root, env=env, check=True)

    # Environment to explore the demo (the fake Slurm comes first in PATH), for POSIX
    # shells (bash, zsh) and fish
    tui = REPO / "tui" / "target" / "release"
    variables = ("FAKESLURM_DIR", "FAKESLURM_EXEC", "DAWGZ_DIR")

    lines = [f"export PATH={shlex.quote(f'{FAKESLURM}:{python}:{tui}')}:$PATH"]
    lines += [f"export {k}={shlex.quote(env[k])}" for k in variables]
    (root / "env.sh").write_text("\n".join(lines) + "\n")

    lines = [f"set -gx PATH {fish_quote(FAKESLURM)} {fish_quote(python)} {fish_quote(tui)} $PATH"]
    lines += [f"set -gx {k} {fish_quote(env[k])}" for k in variables]
    (root / "env.fish").write_text("\n".join(lines) + "\n")

    # The shell cannot be detected reliably (e.g. fish started from a bash login)
    print(f"\nDemo workflows are running on a fake Slurm in {root}")
    print("Explore them with:\n")
    print(f"  source {root / 'env.sh'}    # bash, zsh")
    print(f"  source {root / 'env.fish'}  # fish")
    print("  dawgz")
    print("  dawgz tui")


if __name__ == "__main__":
    main()
