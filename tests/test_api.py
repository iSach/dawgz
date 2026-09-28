"""Tests for the Python interface: factories, operators, backends and scheduling order."""

import pytest
import time

from pathlib import Path

import dawgz

from dawgz import store


@dawgz.job
def append(path: str, x: object) -> None:
    with open(path, "a") as f:
        f.write(f"{x}\n")


@dawgz.job(cpus=2, name="renamed")
def configured(x: int) -> None:
    pass


def test_factory_options() -> None:
    job = configured(1)
    assert job.name == "renamed"
    assert job.settings == {"cpus": 2}

    other = configured.options(cpus=8, partition="gpu")(1)
    assert other.settings == {"cpus": 8, "partition": "gpu"}
    assert other.name == "renamed"

    assert "configured" in repr(configured)


def test_factory_map(tmp_path: Path) -> None:
    jobs = append.map([str(tmp_path / "a")] * 3, [1, 2, 3])
    assert len(jobs) == 3
    assert [repr(j) for j in jobs][0].startswith("append(")

    array = append.map([str(tmp_path / "a")] * 3, [1, 2, 3], array=True, throttle=2)
    assert isinstance(array, dawgz.JobArray)
    assert str(array) == "append[0-2%2]"


def test_rshift(tmp_path: Path) -> None:
    out = str(tmp_path / "order")
    a, b, c, d = (append(out, x) for x in "abcd")

    a >> [b, c] >> d

    assert set(b.dependencies) == {a}
    assert set(c.dependencies) == {a}
    assert set(d.dependencies) == {b, c}

    dawgz.schedule(d, backend="local", quiet=True)
    lines = Path(out).read_text().split()
    assert lines[0] == "a" and lines[-1] == "d"


def test_sequential_order(tmp_path: Path) -> None:
    out = str(tmp_path / "order")
    jobs = [append(out, i) for i in range(6)]
    jobs[5].after(jobs[0])
    jobs[1].after(jobs[4])

    # Dependencies first, then creation order
    dawgz.schedule(*jobs, backend="local", quiet=True)
    assert Path(out).read_text().split() == ["0", "2", "3", "4", "1", "5"]


def test_parallel_workers() -> None:
    nap = dawgz.job(lambda: time.sleep(0.5), name="nap")

    start = time.perf_counter()
    dawgz.schedule(*[nap() for _ in range(4)], backend="local", workers=4, quiet=True)
    assert time.perf_counter() - start < 1.8


def test_default_backend(monkeypatch: pytest.MonkeyPatch, fake_slurm: Path) -> None:
    job = dawgz.job(lambda: None, name="x")()
    assert dawgz.schedule(job, quiet=True).backend == "local"

    monkeypatch.setenv("DAWGZ_BACKEND", "slurm")
    job = dawgz.job(lambda: None, name="x")()
    scheduler = dawgz.schedule(job, workers=4, quiet=True)  # local options are ignored
    assert scheduler.backend == "slurm"


def test_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        dawgz.schedule(dawgz.job(lambda: None, name="x")(), backend="kubernetes")


def test_invalid_arguments() -> None:
    job = dawgz.job(lambda: None, name="x")()
    with pytest.raises(ValueError, match="status"):
        job.after(job, status="sucess")
    with pytest.raises(ValueError, match="mode"):
        job.waitfor("some")
    with pytest.raises(ValueError, match="status"):
        job.mark("done")
    with pytest.raises(ValueError, match="throttle"):
        dawgz.array(job, throttle=0)
    with pytest.raises(TypeError, match="should be jobs"):
        job.after("not a job")


def test_waitfor_any_without_dependencies() -> None:
    job = dawgz.job(lambda: print("ran"), name="alone")().waitfor("any")
    scheduler = dawgz.schedule(job, backend="local", quiet=True)
    assert scheduler.state(job) == "COMPLETED"


def test_reschedule_same_jobs() -> None:
    a = dawgz.job(lambda: None, name="a")().mark("success")
    b = dawgz.job(lambda: None, name="b")().after(a)

    dawgz.schedule(b, backend="local", quiet=True)
    scheduler = dawgz.schedule(b, backend="local", quiet=True)

    assert scheduler.state(b) == "COMPLETED"


def test_source_unavailable() -> None:
    namespace = {}
    exec("def dynamic():\n    print('dynamic')\n", namespace)

    job = dawgz.job(namespace["dynamic"])()
    assert job.source == ""

    scheduler = dawgz.schedule(job, backend="local", quiet=True)
    assert scheduler.logs(job) == "dynamic"


def test_none_settings_are_omitted(fake_slurm: Path) -> None:
    job = dawgz.job(lambda: None, name="x", partition=None, account="me")()
    scheduler = dawgz.schedule(job, backend="slurm", quiet=True)
    content = (scheduler.path / "0000_x.sh").read_text()
    assert "partition" not in content
    assert "--account=me" in content


def test_pack_throttle(fake_slurm: Path) -> None:
    jobs = [dawgz.job(lambda: None, name="x")() for _ in range(10)]
    scheduler = dawgz.schedule(*jobs, backend="slurm", throttle=3, quiet=True)
    content = (scheduler.path / "0000_x.pack.sh").read_text()
    assert "--array=0-9%3" in content


def test_conflicting_settings(fake_slurm: Path) -> None:
    job = dawgz.job(lambda: None, name="x", ram="1G", memory="2G")()
    scheduler = dawgz.schedule(job, backend="slurm", quiet=True)
    assert "conflicting settings" in scheduler.traces[job]


def test_monitoring_while_running() -> None:
    @dawgz.job
    def check() -> None:
        # The workflow is recorded before its jobs run
        rows = store.registry(dawgz.get_dawgz_dir())
        view = store.Workflow.open(dawgz.get_dawgz_dir() / rows[-1]["uid"])
        print(view.summary(view.jobs[0])["state"])

    job = check()
    scheduler = dawgz.schedule(job, backend="local", quiet=True)
    assert scheduler.logs(job) == "RUNNING"


def test_interrupted_workflow_is_detected() -> None:
    scheduler = dawgz.schedule(dawgz.job(lambda: None, name="x")(), backend="local", quiet=True)

    # Simulate a scheduler that died while jobs were running
    state = store.read_json(scheduler.path / "state.json")
    state["finished"] = False
    state["jobs"]["0"]["state"] = "RUNNING"
    store.write_json(scheduler.path / "state.json", state)
    meta = store.read_json(scheduler.path / "workflow.json")
    meta["pid"] = 2**22 + 12345  # not a live process
    store.write_json(scheduler.path / "workflow.json", meta)
    (scheduler.path / "0000_x.run.json").unlink()

    view = store.Workflow.open(scheduler.path)
    assert view.summary(view.jobs[0])["state"] == "CANCELLED"


def test_import_is_lazy() -> None:
    import subprocess
    import sys

    code = "import sys, dawgz, dawgz.cli; print(sorted(m for m in ('cloudpickle', 'asyncio', 'rich') if m in sys.modules))"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]"


def test_spawn_start(tmp_path: Path) -> None:
    out = str(tmp_path / "spawned")
    jobs = [append(out, i) for i in range(3)]
    jobs[2].after(jobs[0], jobs[1])

    scheduler = dawgz.schedule(*jobs, backend="local", start="spawn", workers=2, quiet=True)

    assert sorted(Path(out).read_text().split()) == ["0", "1", "2"]
    assert Path(out).read_text().split()[-1] == "2"
    assert scheduler.logs(jobs[0]) == ""
    assert all(scheduler.state(j) == "COMPLETED" for j in jobs)
    assert not list(scheduler.path.glob("*.local.pkl"))
