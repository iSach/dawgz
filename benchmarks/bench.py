#!/usr/bin/env python
r"""Submission and monitoring benchmark against a fake Slurm.

The fake `sbatch` and `sacct` in `benchmarks/shims` sleep for a fixed latency
(50 ms by default) and never touch a real cluster. Each run uses a fresh DAWGZ_DIR.

    python benchmarks/bench.py --python .venv/bin/python --runs 3

"Submit" times `python workload.py`, "cold" the first `dawgz <workflow>` after
deleting any on-disk state cache, and "warm" the call right after.
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

from pathlib import Path

HERE = Path(__file__).resolve().parent
SHIMS = HERE / "shims"

WORKLOADS = {
    "independent": """
import dawgz

@dawgz.job(cpus=1, time="5:00")
def task(i: int) -> None:
    print(i)

if __name__ == "__main__":
    dawgz.schedule(*[task(i) for i in range({n})], backend="slurm", name="bench")
""",
    "fan-in": """
import dawgz

@dawgz.job(cpus=1, time="5:00")
def task(i: int) -> None:
    print(i)

@dawgz.job(cpus=1, time="5:00")
def merge() -> None:
    print("merge")

if __name__ == "__main__":
    dawgz.schedule(merge().after(*[task(i) for i in range({n})]), backend="slurm", name="bench")
""",
    "array": """
import dawgz

@dawgz.job(cpus=1, time="5:00")
def task(i: int) -> None:
    print(i)

if __name__ == "__main__":
    dawgz.schedule(dawgz.array(*[task(i) for i in range({n})]), backend="slurm", name="bench")
""",
}


def run(cmd: list[str], env: dict, calls: Path) -> dict:
    before = calls.read_text().count("sacct") if calls.exists() else 0
    start = time.perf_counter()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    seconds = time.perf_counter() - start
    after = calls.read_text().count("sacct") if calls.exists() else 0

    if proc.returncode != 0:
        print(proc.stdout, proc.stderr, file=sys.stderr)

    return {"seconds": round(seconds, 4), "sacct": after - before, "exit": proc.returncode}


def clear_cache(dawgz_dir: Path) -> None:
    for pattern in ("*/state.json", "*/state.lock", "sacct*.json"):
        for file in dawgz_dir.glob(pattern):
            file.unlink()


def bench(python: str, workload: str, n: int, runs: int) -> dict:
    results = []

    for _ in range(runs):
        with tempfile.TemporaryDirectory(dir=os.environ.get("BENCH_TMP")) as tmp:
            tmp = Path(tmp)
            (tmp / "slurm").mkdir()
            script = tmp / "workload.py"
            script.write_text(WORKLOADS[workload].format(n=n))

            bindir = str(Path(python).parent)
            env = {
                **os.environ,
                "PATH": f"{SHIMS}:{bindir}:{os.environ['PATH']}",
                "DAWGZ_DIR": str(tmp / ".dawgz"),
                "FAKESLURM_DIR": str(tmp / "slurm"),
                "NO_COLOR": "1",
            }

            # Never reach the real Slurm
            assert shutil.which("sbatch", path=env["PATH"]) == str(SHIMS / "sbatch")
            assert shutil.which("sacct", path=env["PATH"]) == str(SHIMS / "sacct")

            calls = tmp / "slurm" / "calls"
            dawgz = [python, "-m", "dawgz"]

            submit = run([python, str(script)], env, calls)
            clear_cache(tmp / ".dawgz")
            cold = run([*dawgz, "0"], env, calls)
            warm = run([*dawgz, "0"], env, calls)
            listing = run(dawgz, env, calls)

            results.append({"submit": submit, "cold": cold, "warm": warm, "list": listing})

    return {
        key: {
            "seconds": statistics.median(r[key]["seconds"] for r in results),
            "sacct": statistics.median(r[key]["sacct"] for r in results),
            "exit": max(r[key]["exit"] for r in results),
        }
        for key in results[0]
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=50)
    parser.add_argument("--workloads", nargs="+", default=["independent"])
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    report = {}

    for workload in args.workloads:
        report[workload] = bench(args.python, workload, args.jobs, args.runs)

        print(f"{workload} ({args.jobs} jobs)")
        for key, value in report[workload].items():
            print(
                f"  {key:<7} {value['seconds']:7.3f} s   sacct={value['sacct']:<3g} exit={value['exit']}"
            )

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
