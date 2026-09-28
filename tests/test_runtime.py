"""Tests for the job runtime: log cooking, progress reporting and stream capture."""

import os
import pytest
import subprocess
import sys
import time

from pathlib import Path

import dawgz

from dawgz import store
from dawgz.runtime import Cooker, Run, parse_bar
from dawgz.utils import cat


def cook(chunks: list[bytes], interval: float = 1e9) -> bytes:
    r, w = os.pipe()
    cooker = Cooker(w, interval=interval)
    for chunk in chunks:
        cooker.feed(chunk)
    cooker.close()
    os.close(w)
    with os.fdopen(r, "rb") as f:
        return f.read()


def test_cooker_passes_lines_through() -> None:
    assert cook([b"hello\n", b"wor", b"ld\n", b"tail"]) == b"hello\nworld\ntail"


def test_cooker_collapses_redraws() -> None:
    chunks = [b"start\n"] + [f"\r{i:3d}%".encode() for i in range(101)] + [b"\ndone\n"]
    out = cook(chunks)

    assert out.count(b"\r") <= 2
    assert cat(out.decode(), -1) == "start\n100%\ndone\n"


def test_cooker_checkpoints_redraws() -> None:
    out = cook([b"\r 10%", b"\r 20%", b"\r 30%", b"\n"], interval=0.0)

    assert b" 10%" in out and b" 30%" in out
    assert cat(out.decode(), -1) == " 30%\n"


def test_cooker_flushes_pending_redraw() -> None:
    out = cook([b"\r 10%", b"\r 99%"])
    assert cat(out.decode(), -1).strip() == "99%"


@pytest.mark.parametrize(
    "line, expected",
    [
        (
            "epoch 1:  45%|████▌     | 45/100 [00:10<00:12,  4.50it/s, loss=0.2]",
            {
                "desc": "epoch 1",
                "n": 45,
                "total": 100,
                "rate": 4.5,
                "eta": 12.0,
                "postfix": "loss=0.2",
            },
        ),
        (
            " 12%|█▏        | 1.20k/10.0k [00:01<00:07, 1.10kit/s]",
            {"desc": "progress", "n": 1200, "total": 10000},
        ),
        (
            "train: 100%|██████████| 3/3 [00:03<00:00,  1.00s/it]",
            {"desc": "train", "n": 3, "total": 3, "rate": 1.0},
        ),
        ("steps: 42it [00:02, 20.00it/s]", {"desc": "steps", "n": 42, "rate": 20.0}),
    ],
)
def test_parse_tqdm(line: str, expected: dict) -> None:
    bar = parse_bar(line)
    assert bar is not None
    for key, value in expected.items():
        assert bar[key] == pytest.approx(value), key


def test_parse_non_bars() -> None:
    assert parse_bar("loss 0.5 at step 10/100") is None
    assert parse_bar("hello world") is None


def test_run_throttles_writes(tmp_path: Path) -> None:
    run = Run(tmp_path / "x.run.json")
    for i in range(1000):
        run.bar("loop", n=i, total=1000)
    run.bar("loop", n=1000, total=1000)
    run.finish("COMPLETED")

    data = store.read_json(tmp_path / "x.run.json")
    assert data["state"] == "COMPLETED"
    assert data["progress"] == [
        {"desc": "loop", "n": 1000, "total": 1000, "t": data["progress"][0]["t"]}
    ]


@dawgz.job
def tqdm_like() -> None:
    for i in range(1, 2001):
        sys.stderr.write(
            f"\rtrain: {i // 20:3d}%|#####     | {i}/2000 [00:01<00:01, 1000.00it/s, loss=0.5]"
        )
        sys.stderr.flush()
    sys.stderr.write("\n")
    print("finished")


def test_tqdm_logs_are_compact_and_parsed() -> None:
    scheduler = dawgz.schedule(tqdm_like(), backend="local", quiet=True)

    logfile = store.log_file(scheduler.path, "0000_tqdm_like")
    run = store.read_json(store.run_file(scheduler.path, "0000_tqdm_like"))

    # 2000 redraws of ~80 bytes would take 160 kB
    assert logfile.stat().st_size < 1000
    assert scheduler.logs(scheduler.order and next(iter(scheduler.order))).endswith("finished")

    bar = run["progress"][0]
    assert bar["desc"] == "train"
    assert bar["n"] == 2000
    assert bar["total"] == 2000
    assert bar["postfix"] == "loss=0.5"


@dawgz.job
def low_level() -> None:
    os.write(1, b"from fd 1\n")
    os.write(2, b"from fd 2\n")
    subprocess.run(["echo", "from a subprocess"], check=True)
    print("from print")


def test_captures_file_descriptors() -> None:
    job = low_level()
    scheduler = dawgz.schedule(job, backend="local", quiet=True)

    logs = scheduler.logs(job)
    for text in ("from fd 1", "from fd 2", "from a subprocess", "from print"):
        assert text in logs


@pytest.mark.parametrize("code, state", [(0, "COMPLETED"), (None, "COMPLETED"), (3, "FAILED")])
def test_system_exit(code: int | None, state: str) -> None:
    @dawgz.job
    def leave() -> None:
        sys.exit(code)

    job = leave()
    after = dawgz.job(lambda: print("after"), name="after")().after(job, status="any")
    scheduler = dawgz.schedule(after, backend="local", quiet=True)

    assert scheduler.state(job) == state
    assert scheduler.state(after) == "COMPLETED"


def test_raw_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAWGZ_RAW_LOGS", "1")

    job = dawgz.job(lambda: print("\r10%\r20%"), name="raw")()
    scheduler = dawgz.schedule(job, backend="local", quiet=True)

    raw = store.log_file(scheduler.path, "0000_raw").read_bytes()
    assert raw == b"\r10%\r20%\n"


def test_progress_outside_dawgz(capsys: pytest.CaptureFixture) -> None:
    total = 0
    for x in dawgz.progress(range(5), desc="plain"):
        total += x
    assert total == 10

    with dawgz.Progress(total=3) as bar:
        bar.update(3, loss=0.1)
    dawgz.status("hello")

    assert "plain" not in capsys.readouterr().err  # not a terminal


def test_progress_tty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("DAWGZ_TTY", "1")
    for _ in dawgz.progress(range(3), desc="shown"):
        pass
    err = capsys.readouterr().err
    assert "shown" in err
    assert "3/3" in err


def test_runner_without_dawgz(tmp_path: Path, fake_slurm: Path) -> None:
    # The generated run.py falls back to plain pickle when dawgz is not importable
    scheduler = dawgz.schedule(
        *[dawgz.job(lambda: print("packed"), name="p")() for _ in range(2)],
        backend="slurm",
        quiet=True,
    )

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["SLURM_ARRAY_TASK_ID"] = "1"
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "dawgz.py").write_text("raise ImportError('no dawgz here')\n")
    env["PYTHONPATH"] = str(stub)

    subprocess.run(
        [sys.executable, str(scheduler.path / "run.py"), "--pack", "0000_p.pack"],
        env=env,
        check=True,
        cwd=tmp_path,
    )

    assert store.log_file(scheduler.path, "0001_p").read_text() == "packed\n"


def test_nested_tqdm_bars() -> None:
    tqdm = pytest.importorskip("tqdm")

    @dawgz.job
    def nested() -> None:
        for _ in tqdm.tqdm(range(3), desc="epochs", mininterval=0):
            for _ in tqdm.tqdm(range(200), desc="batches", mininterval=0, leave=False):
                pass
        print("done")

    job = nested()
    scheduler = dawgz.schedule(job, backend="local", quiet=True)

    logfile = store.log_file(scheduler.path, "0000_nested")
    lines = cat(logfile.read_text(), -1).strip().splitlines()

    # 600 redraws of the inner bar would write 600 lines
    assert len(lines) < 10
    assert lines[-1] == "done"
    assert "\x1b[A" not in logfile.read_text()

    run = store.read_json(store.run_file(scheduler.path, "0000_nested"))
    descs = {bar["desc"] for bar in run["progress"]}
    assert "epochs" in descs


def test_malformed_bar_does_not_hang() -> None:
    @dawgz.job
    def weird() -> None:
        sys.stderr.write("\rcopy: 5%|#| 1.2.3/10 [00:01<00:02, 1.00it/s]")
        for i in range(5000):
            print(f"line {i}")

    job = weird()
    start = time.time()
    scheduler = dawgz.schedule(job, backend="local", quiet=True)

    assert time.time() - start < 10
    assert scheduler.state(job) == "COMPLETED"
    assert scheduler.logs(job).endswith("line 4999")


def test_leftover_process_does_not_corrupt_records() -> None:
    @dawgz.job
    def spawn() -> None:
        subprocess.Popen([
            "sh",
            "-c",
            "for i in 1 2 3 4 5 6 7 8 9 10; do echo daemon; sleep 0.1; done",
        ])
        print("parent done")

    job = spawn()
    scheduler = dawgz.schedule(job, backend="local", quiet=True)

    run = store.read_json(store.run_file(scheduler.path, "0000_spawn"))
    assert run is not None and run["state"] == "COMPLETED"

    time.sleep(1.5)
    run = store.read_json(store.run_file(scheduler.path, "0000_spawn"))
    assert run is not None and run["state"] == "COMPLETED"
    assert "parent done" in scheduler.logs(job)


def test_changing_descriptions_are_bounded() -> None:
    @dawgz.job
    def moving() -> None:
        with dawgz.Progress(total=100, desc="loss ?") as bar:
            for i in range(100):
                bar.set_description(f"loss {1 / (i + 1):.3f}")
                bar.update()
        for i in range(50):
            sys.stderr.write(f"\rstep {i}: {2 * i:3d}%|#| {i}/50 [00:00<00:00, 9.99it/s]")
        sys.stderr.write("\n")

    scheduler = dawgz.schedule(moving(), backend="local", quiet=True)
    run = store.read_json(store.run_file(scheduler.path, "0000_moving"))

    assert len(run["progress"]) <= 2
    assert any(bar["desc"] == "loss 0.010" for bar in run["progress"])


def test_exit_code_out_of_range() -> None:
    job = dawgz.job(lambda: sys.exit(256), name="big_exit")()
    scheduler = dawgz.schedule(job, backend="local", quiet=True)
    assert scheduler.state(job) == "FAILED"
