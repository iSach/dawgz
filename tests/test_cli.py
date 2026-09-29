"""Tests for the dawgz CLI."""

import json
import pytest
import sys
import time

from pathlib import Path

import dawgz

from dawgz import store
from dawgz.cli import main
from dawgz.graph import groups, lanes

from .conftest import sacct_calls

########
# Jobs #
########


@dawgz.job
def noop() -> int:
    print("42")


@dawgz.job
def failing(arg: str, kwarg: str) -> None:
    raise RuntimeError("intentional failure")


@dawgz.job
def echo(msg: str) -> None:
    print(msg)


@dawgz.job
def bar() -> None:
    for _ in dawgz.progress(range(5), desc="steps"):
        pass


############
# Fixtures #
############


@pytest.fixture()
def dummy_workflow() -> dawgz.Scheduler:
    return dawgz.schedule(
        noop(),
        failing("a", kwarg="k"),
        echo("[bracket] hello"),
        name="dummy",
        backend="async",
        quiet=True,
    )


def run(capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


#########
# Tests #
#########


def test_main_no_workflows(capsys: pytest.CaptureFixture) -> None:
    code, out, _ = run(capsys)
    assert code == 0
    assert "no workflow" in out


def test_main_workflows(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    code, out, _ = run(capsys)
    assert code == 0
    assert "dummy" in out
    assert "async" in out
    assert dummy_workflow.uid in out


def test_main_workflow(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    code, out, _ = run(capsys, "0")
    assert code == 0
    assert "noop" in out
    assert "completed" in out
    assert "failing" in out
    assert "failed" in out
    assert "echo" in out


def test_workflow_references(
    dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture
) -> None:
    for ref in ("0", "-1", "dummy", dummy_workflow.uid, dummy_workflow.uid[:10]):
        code, out, _ = run(capsys, ref)
        assert code == 0, ref
        assert "noop" in out


def test_main_job(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    code, out, _ = run(capsys, "0", "0")
    assert code == 0
    assert "noop" in out
    assert "completed" in out
    assert "42" in out


def test_job_by_name(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    _, out, _ = run(capsys, "dummy", "echo")
    assert "[bracket] hello" in out


def test_main_job_failing(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    _, out, _ = run(capsys, "-1", "1")
    assert "failing" in out
    assert "failed" in out
    assert "RuntimeError" in out


def test_main_job_source(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    _, out, _ = run(capsys, "-1", "-2", "--source")
    assert "failing" in out
    assert "def failing" in out


def test_main_job_input(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    _, out, _ = run(capsys, "0", "-2", "--input")
    assert "failing('a', kwarg='k')" in out


def test_main_job_logs_preserve_brackets(
    dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture
) -> None:
    _, out, _ = run(capsys, "-1", "-1")
    assert "echo" in out
    assert "[bracket] hello" in out


def test_main_job_raw(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    _, out, _ = run(capsys, "0", "0", "--raw")
    assert out == "42\n"


def test_logs_command(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    _, out, _ = run(capsys, "logs", "0", "0")
    assert out == "42\n"


def test_main_invalid_workflow_index(
    dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture
) -> None:
    code, _, err = run(capsys, "99")
    assert code == 2
    assert "out of range" in err


def test_main_invalid_job_index(
    dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture
) -> None:
    code, _, err = run(capsys, "0", "99")
    assert code == 2
    assert "out of range" in err


def test_json(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    _, out, _ = run(capsys, "--json")
    rows = json.loads(out)
    assert rows[0]["uid"] == dummy_workflow.uid
    assert rows[0]["totals"]["done"] == 2
    assert rows[0]["totals"]["failed"] == 1

    _, out, _ = run(capsys, "0", "--json")
    data = json.loads(out)
    assert [j["summary"]["state"] for j in data["jobs"]] == ["COMPLETED", "FAILED", "COMPLETED"]


def test_progress_is_shown(capsys: pytest.CaptureFixture) -> None:
    scheduler = dawgz.schedule(bar(), backend="local", quiet=True)

    run_file = store.read_json(store.run_file(scheduler.path, "0000_bar"))
    assert run_file["progress"][0]["n"] == 5

    _, out, _ = run(capsys, "0", "0")
    assert "steps" in out
    assert "5/5" in out


def test_fan_out_grouping(capsys: pytest.CaptureFixture) -> None:
    tasks = echo.map([str(i) for i in range(10)])
    merge = noop().after(*tasks)

    dawgz.schedule(merge, backend="local", quiet=True, workers=4)

    _, out, _ = run(capsys, "0")
    assert "echo ×10" in out
    assert "0-9" in out

    _, out, _ = run(capsys, "0", "--expand")
    assert "echo ×10" not in out
    assert out.count("echo") >= 10


def test_slurm_listing_uses_one_sacct(fake_slurm: Path, capsys: pytest.CaptureFixture) -> None:
    for _ in range(3):
        dawgz.schedule(*echo.map(["a", "b", "c"]), backend="slurm", quiet=True)

    # Expire the caches written at submission
    for path in (dawgz.get_dawgz_dir()).iterdir():
        if (path / "state.json").exists():
            (path / "state.json").unlink()

    before = sacct_calls(fake_slurm)
    code, _, _ = run(capsys)
    assert code == 0
    assert sacct_calls(fake_slurm) == before + 1

    # Warm: no query
    run(capsys)
    run(capsys, "0")
    assert sacct_calls(fake_slurm) == before + 1

    # Offline: never
    run(capsys, "0", "--refresh", "--offline")
    assert sacct_calls(fake_slurm) == before + 1

    # Forced
    run(capsys, "0", "--refresh")
    assert sacct_calls(fake_slurm) == before + 2


def test_legacy_workflow(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    # Workflows recorded by dawgz < 3 only have a pickle
    (dummy_workflow.path / "workflow.json").unlink()
    (dummy_workflow.path / "state.json").unlink()

    _, out, _ = run(capsys, "0")
    assert "noop" in out
    assert "failed" in out
    assert (dummy_workflow.path / "workflow.json").exists()


def test_du(dummy_workflow: dawgz.Scheduler, capsys: pytest.CaptureFixture) -> None:
    code, out, _ = run(capsys, "du")
    assert code == 0
    assert "dummy" in out
    assert "LOGS" in out


def test_clean(capsys: pytest.CaptureFixture) -> None:
    for i in range(4):
        dawgz.schedule(echo(str(i)), backend="local", name=f"w{i}", quiet=True)

    code, out, _ = run(capsys, "clean", "--keep", "2", "--dry-run")
    assert code == 0
    assert "w0" in out and "w1" in out and "w2" not in out
    assert len(store.registry(dawgz.get_dawgz_dir())) == 4

    code, out, _ = run(capsys, "clean", "--keep", "2", "-y")
    assert code == 0
    rows = store.registry(dawgz.get_dawgz_dir())
    assert [r["name"] for r in rows] == ["w2", "w3"]
    assert len([p for p in dawgz.get_dawgz_dir().iterdir() if p.is_dir()]) == 2

    code, out, _ = run(capsys, "clean", "--older-than", "1d", "-y")
    assert "nothing to clean" in out

    code, _, _ = run(capsys, "clean")
    assert code == 2


def test_clean_skips_active(fake_slurm: Path, capsys: pytest.CaptureFixture) -> None:
    dawgz.schedule(echo("a"), backend="slurm", quiet=True)

    _, _, err = run(capsys, "clean", "--keep", "0", "-y")
    assert "skipping active workflow" in err
    assert len(store.registry(dawgz.get_dawgz_dir())) == 1


def test_shrink_logs(capsys: pytest.CaptureFixture) -> None:
    @dawgz.job
    def spam() -> None:
        for i in range(20000):
            print(f"line {i}")

    scheduler = dawgz.schedule(spam(), backend="local", quiet=True)
    logfile = store.log_file(scheduler.path, "0000_spam")
    size = logfile.stat().st_size

    code, _, _ = run(capsys, "clean", "0", "--shrink-logs", "10k")
    assert code == 0
    assert logfile.stat().st_size < 12 * 1024 < size

    text = logfile.read_text()
    assert text.startswith("line 0\n")
    assert text.rstrip().endswith("line 19999")
    assert "truncated" in text


def test_cancel_slurm(fake_slurm: Path, capsys: pytest.CaptureFixture) -> None:
    dawgz.schedule(*echo.map(["a", "b"]), backend="slurm", quiet=True)

    code, out, _ = run(capsys, "cancel", "0")
    assert code == 0
    assert "Terminating" in out

    _, out, _ = run(capsys, "0")
    assert "cancelled" in out

    # Legacy flag
    _, out, _ = run(capsys, "0", "-c")
    assert "nothing to cancel" in out


def test_help(capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["dawgz", "--help"])
    with pytest.raises(SystemExit):
        main()
    assert "Monitor dawgz workflows" in capsys.readouterr().out


def test_lanes() -> None:
    #   0
    #  / \
    # 1   2
    #  \ /
    #   3
    rows = lanes([0, 1, 2, 3], {1: [0], 2: [0], 3: [1, 2]})
    text = ["".join(r).rstrip() for r in rows]
    assert text == ["@─╮", "@ │", "│ @", "@─╯"]

    # Chain
    rows = lanes([0, 1, 2], {1: [0], 2: [1]})
    assert ["".join(r).rstrip() for r in rows] == ["@", "@", "@"]


def test_groups() -> None:
    jobs = [{"index": 0, "name": "prep", "deps": []}]
    jobs += [{"index": i, "name": "task", "deps": [[0, "success"]]} for i in range(1, 6)]
    jobs += [{"index": 6, "name": "merge", "deps": [[i, "success"] for i in range(1, 6)]}]

    result = groups(jobs)
    assert [len(g) for g in result] == [1, 5, 1]


def test_watch_mode_is_bounded(
    dummy_workflow: dawgz.Scheduler, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    calls = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", sleep)
    code, out, _ = run(capsys, "0", "--watch", "1")
    assert code == 0
    assert out.count("noop") >= 2


def test_cancel_local(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    import os
    import subprocess

    script = tmp_path / "long.py"
    script.write_text(
        "import time\n"
        "import dawgz\n"
        "@dawgz.job\n"
        "def nap(i):\n"
        "    time.sleep(60)\n"
        "dawgz.schedule(*nap.map(range(3)), backend='local', workers=2, quiet=True)\n"
    )
    env = {**os.environ, "DAWGZ_DIR": str(dawgz.get_dawgz_dir())}
    proc = subprocess.Popen([sys.executable, str(script)], env=env)

    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            rows = store.registry(dawgz.get_dawgz_dir())
            if (
                rows
                and len(list((dawgz.get_dawgz_dir() / rows[0]["uid"]).glob("*.run.json"))) == 2
            ):
                break
            time.sleep(0.1)

        code, out, _ = run(capsys, "cancel", "0")
        assert code == 0
        assert "SIGTERM" in out

        proc.wait(timeout=20)
    finally:
        proc.kill()

    _, out, _ = run(capsys, "0", "--json")
    states = [j["summary"]["state"] for j in json.loads(out)["jobs"]]
    assert states == ["CANCELLED"] * 3

    _, out, _ = run(capsys, "cancel", "0")
    assert "nothing to cancel" in out


def test_cancel_local_kills_subprocesses(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    import os
    import subprocess

    marker = tmp_path / "pid"
    script = tmp_path / "long.py"
    script.write_text(
        "import subprocess, time\n"
        "import dawgz\n"
        "@dawgz.job\n"
        "def nap():\n"
        f"    p = subprocess.Popen(['sleep', '60']); open({str(marker)!r}, 'w').write(str(p.pid)); p.wait()\n"
        "dawgz.schedule(nap(), backend='local', quiet=True)\n"
    )
    env = {**os.environ, "DAWGZ_DIR": str(dawgz.get_dawgz_dir())}
    proc = subprocess.Popen([sys.executable, str(script)], env=env)

    try:
        deadline = time.time() + 20
        while not (marker.exists() and marker.read_text()) and time.time() < deadline:
            time.sleep(0.05)
        sleeper = int(marker.read_text())

        run(capsys, "cancel", "0")
        proc.wait(timeout=20)

        time.sleep(0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(sleeper, 0)
    finally:
        proc.kill()


def test_destructive_commands_need_unambiguous_references(capsys: pytest.CaptureFixture) -> None:
    for _ in range(2):
        dawgz.schedule(noop(), backend="local", name="same", quiet=True)

    code, _, err = run(capsys, "clean", "same", "-y")
    assert code == 2
    assert "matches 2 workflows" in err
    assert len(store.registry(dawgz.get_dawgz_dir())) == 2

    code, _, _ = run(capsys, "same")  # showing picks the most recent
    assert code == 0


def test_registry_ignores_unsafe_ids(capsys: pytest.CaptureFixture) -> None:
    dawgz.schedule(noop(), backend="local", name="ok", quiet=True)
    with open(dawgz.get_dawgz_dir() / "workflows.csv", "a") as f:
        f.write("evil,..,2026-01-01,local,1,0\nempty,,2026-01-01,local,1,0\n")

    assert [r["name"] for r in store.registry(dawgz.get_dawgz_dir())] == ["ok"]

    code, _, _ = run(capsys, "clean", "--keep", "0", "-y")
    assert code == 0
    assert dawgz.get_dawgz_dir().exists()
