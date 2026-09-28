"""Tests for the 'slurm' backend, against the fake Slurm in `tools/fakeslurm`."""

import os
import pytest
import time

from collections.abc import Callable
from pathlib import Path

import dawgz

from dawgz import sacct, store

from .conftest import sacct_calls

########
# Jobs #
########


@dawgz.job
def echo(x: object) -> None:
    print(repr(x))


@dawgz.job
def fail() -> None:
    raise RuntimeError("intentional failure")


#########
# Tests #
#########


def test_shfile() -> None:
    def hello() -> None:
        pass

    job = dawgz.job(
        hello,
        name="hello",
        shell="/bin/zsh",
        interpreter="python3",
        env=["export FOO=bar", "module load cuda"],
        settings={
            "cpus": 4,
            "ram": "16GB",
            "partition": "gpu",
            "tasks_per_node": 8,
            "gpus_per_node": 8,
            "exclusive": True,
            "requeue": False,
        },
    )()

    scheduler = dawgz.schedule(job, backend="slurm")

    tag = scheduler.tag(job)
    shfile = scheduler.path / f"{tag}.sh"
    content = shfile.read_text()

    # Shell
    assert content.startswith("#!/bin/zsh\n")

    # Name
    assert f"--job-name={tag}" in content

    # Env
    assert "export FOO=bar\n" in content
    assert "module load cuda\n" in content

    # Interpreter
    assert "srun python3 " in content
    assert content.rstrip().endswith(f"run.py {tag}")

    # Settings
    assert "SBATCH --nodes=1" in content
    assert "SBATCH --ntasks-per-node=8" in content
    assert "SBATCH --cpus-per-task=4" in content
    assert "SBATCH --gpus-per-node=8" in content
    assert "SBATCH --mem=16GB" in content
    assert "SBATCH --partition=gpu" in content
    assert "SBATCH --exclusive\n" in content
    assert "requeue" not in content  # False flags are omitted


def test_decorator_options() -> None:
    @dawgz.job(name="renamed", env=["export A=1"], settings={"cpus": 2}, partition="gpu")
    def f() -> None:
        pass

    job = f()
    scheduler = dawgz.schedule(job, backend="slurm")
    content = (scheduler.path / f"{scheduler.tag(job)}.sh").read_text()

    assert job.name == "renamed"
    assert "export A=1" in content
    assert "--cpus-per-task=2" in content
    assert "--partition=gpu" in content


@pytest.mark.parametrize("wait_mode", ["all", "any"])
@pytest.mark.parametrize("status", ["success", "failure"])
def test_dependencies(wait_mode: str, status: str) -> None:
    a_job = echo("a")
    b_job = echo("b")
    c_job = echo("c").after(a_job, b_job, status=status).waitfor(wait_mode)

    scheduler = dawgz.schedule(c_job, backend="slurm", pack=False)

    a_jobid = scheduler.results[a_job]
    b_jobid = scheduler.results[b_job]
    c_tag = scheduler.tag(c_job)

    shfile = scheduler.path / f"{c_tag}.sh"
    content = shfile.read_text()

    sep = "," if wait_mode == "all" else "?"
    after = "afterok" if status == "success" else "afternotok"

    assert f"--dependency={after}:{a_jobid}{sep}{after}:{b_jobid}" in content
    assert "--kill-on-invalid-dep=yes" in content


@pytest.mark.parametrize("status", ["success", "failure", "any"])
def test_pack_dependencies(status: str) -> None:
    tasks = echo.map(range(5))
    merge = echo("merge").after(*tasks, status=status)
    first = echo("first").after(tasks[0])

    scheduler = dawgz.schedule(merge, first, backend="slurm")

    # A single submission for the 5 independent tasks
    arrayid = scheduler.results[tasks[0]].split("_")[0]
    assert [scheduler.results[t] for t in tasks] == [f"{arrayid}_{k}" for k in range(5)]

    content = (scheduler.path / f"{scheduler.tag(merge)}.sh").read_text()
    if status == "success":
        assert f"--dependency=afterok:{arrayid}\n" in content
    elif status == "any":
        assert f"--dependency=afterany:{arrayid}\n" in content
    else:
        assert ",".join(f"afternotok:{arrayid}_{k}" for k in range(5)) in content

    content = (scheduler.path / f"{scheduler.tag(first)}.sh").read_text()
    assert f"--dependency=afterok:{arrayid}_0\n" in content


def test_pack_groups_identical_jobs(fake_slurm: Path) -> None:
    small = echo.map(range(3))
    big = [echo.options(cpus=8)(i) for i in range(2)]
    array = dawgz.array(*echo.map(range(2)))

    scheduler = dawgz.schedule(*small, *big, array, backend="slurm")

    calls = [
        line
        for line in (fake_slurm / "calls").read_text().splitlines()
        if line.startswith("sbatch")
    ]
    assert len(calls) == 3

    ids = {scheduler.results[j].split("_")[0] for j in small}
    assert len(ids) == 1
    assert scheduler.results[array].isdigit()


def test_pack_execution(slurm_exec: None, wait_slurm: Callable) -> None:
    tasks = echo.map(["x", "y", "z"]) + [fail()]
    merge = echo("merge").after(*tasks[:3])

    scheduler = dawgz.schedule(merge, tasks[3], backend="slurm")

    wait_slurm()

    for task, x in zip(tasks, "xyz", strict=False):
        assert scheduler.logs(task) == repr(x)
        assert scheduler.state(task) == "COMPLETED"

    assert scheduler.state(tasks[3]) == "FAILED"
    assert "intentional failure" in scheduler.logs(tasks[3])
    assert scheduler.state(merge) == "COMPLETED"
    assert scheduler.logs(merge) == "'merge'"


def test_pack_cancel_one(fake_slurm: Path) -> None:
    tasks = echo.map(range(3))
    scheduler = dawgz.schedule(*tasks, backend="slurm")

    scheduler.cancel(tasks[1])

    assert scheduler.state(tasks[0]) == "PENDING"
    assert scheduler.state(tasks[1]) == "CANCELLED"


def test_no_pack(fake_slurm: Path) -> None:
    scheduler = dawgz.schedule(*echo.map(range(3)), backend="slurm", pack=False)
    assert all(jobid.isdigit() for jobid in scheduler.results.values())


def test_any_satisfied_by_pruned_dependency() -> None:
    a_job = echo("a").mark("success")
    b_job = echo("b")
    c_job = echo("c").after(a_job, b_job).waitfor("any")

    scheduler = dawgz.schedule(c_job, backend="slurm")
    content = (scheduler.path / f"{scheduler.tag(c_job)}.sh").read_text()

    assert "--dependency" not in content
    assert b_job in scheduler.results


def test_any_tolerates_failed_submission() -> None:
    a_job = echo("a")
    b_job = dawgz.job(lambda: None, name="bad", partition="fail")()
    c_job = echo("c").after(a_job, b_job).waitfor("any")

    scheduler = dawgz.schedule(c_job, backend="slurm")
    content = (scheduler.path / f"{scheduler.tag(c_job)}.sh").read_text()

    assert "JobSubmissionError" in scheduler.traces[b_job]
    assert c_job in scheduler.results
    assert f"--dependency=afterok:{scheduler.results[a_job]}\n" in content


def test_dependency_submission_failure() -> None:
    a_job = dawgz.job(lambda: None, name="a", partition="fail")()
    b_job = echo("b").after(a_job)

    scheduler = dawgz.schedule(b_job, backend="slurm")

    assert "JobSubmissionError" in scheduler.logs(a_job)
    assert "invalid partition" in scheduler.logs(a_job)
    assert "JobNeverSatisfiedError" in scheduler.logs(b_job)
    assert scheduler.state(a_job) == "FAILED"
    assert scheduler.state(b_job) == "CANCELLED"


def test_concurrent_submission(fake_slurm: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKESLURM_DELAY", "0.2")

    jobs = echo.map(range(8))

    start = time.perf_counter()
    scheduler = dawgz.schedule(*jobs, backend="slurm", concurrency=8)
    elapsed = time.perf_counter() - start

    assert len(scheduler.results) == 8
    assert elapsed < 8 * 0.2  # sequential submission would take at least 1.6 s


def test_chain_waits_for_ids() -> None:
    jobs = [echo(i) for i in range(4)]
    for a, b in zip(jobs, jobs[1:], strict=False):
        a >> b

    scheduler = dawgz.schedule(jobs[-1], backend="slurm")

    for a, b in zip(jobs, jobs[1:], strict=False):
        content = (scheduler.path / f"{scheduler.tag(b)}.sh").read_text()
        assert f"afterok:{scheduler.results[a]}" in content

    assert [scheduler.order[j] for j in jobs] == [0, 1, 2, 3]


def test_single_job(slurm_exec: None, wait_slurm: Callable) -> None:
    x = "a"
    job = echo(x)
    scheduler = dawgz.schedule(job, backend="slurm")

    wait_slurm()

    assert scheduler.logs(job) == repr(x)
    assert scheduler.state(job) == "COMPLETED"


def test_failing_job(slurm_exec: None, wait_slurm: Callable) -> None:
    job = fail()
    scheduler = dawgz.schedule(job, backend="slurm")

    wait_slurm()

    assert "RuntimeError: intentional failure" in scheduler.logs(job)
    assert scheduler.state(job) == "FAILED"


def test_job_array(slurm_exec: None, wait_slurm: Callable) -> None:
    xs = ["a", "b", "c"]
    array = echo.map(xs, array=True)

    scheduler = dawgz.schedule(array, backend="slurm")

    # A single pickle for the whole array
    assert (scheduler.path / f"{scheduler.tag(array)}.pkl").exists()
    assert len([p for p in scheduler.path.glob("*.pkl") if p.name != "dump.pkl"]) == 1

    wait_slurm()

    for i, x in enumerate(xs):
        assert scheduler.logs(array, i) == repr(x)
        assert scheduler.state(array, i) == "COMPLETED"

    assert scheduler.state(array) == "COMPLETED"


def test_never_satisfied_is_cancelled(slurm_exec: None, wait_slurm: Callable) -> None:
    a_job = fail()
    b_job = echo("b").after(a_job)

    scheduler = dawgz.schedule(b_job, backend="slurm")

    wait_slurm()

    assert scheduler.state(a_job) == "FAILED"
    assert scheduler.state(b_job) == "CANCELLED"


def test_export_env_var(slurm_exec: None, wait_slurm: Callable) -> None:
    def get_env() -> None:
        print(os.environ["DAWGZ_VAR"])

    job = dawgz.job(
        get_env,
        env=[
            "export DAWGZ_VAR='hello, world'",
        ],
    )()

    scheduler = dawgz.schedule(job, backend="slurm")

    wait_slurm()

    assert scheduler.logs(job) == "hello, world"


def test_progress_and_run_file(slurm_exec: None, wait_slurm: Callable, fake_slurm: Path) -> None:
    @dawgz.job
    def count() -> None:
        for _ in dawgz.progress(range(10), desc="count"):
            pass

    job = count()
    scheduler = dawgz.schedule(job, backend="slurm")

    wait_slurm()

    run = store.read_json(store.run_file(scheduler.path, scheduler.tag(job)))

    assert run["state"] == "COMPLETED"
    assert run["progress"][0]["desc"] == "count"
    assert run["progress"][0]["n"] == 10
    assert run["progress"][0]["total"] == 10

    # The state is known from the run file, without querying Slurm
    before = sacct_calls(fake_slurm)
    view = store.Workflow.open(scheduler.path)
    assert view.summary(view.jobs[0])["state"] == "COMPLETED"
    assert sacct.refresh([view], ttl=0) == 0
    assert sacct_calls(fake_slurm) == before


def test_batched_sacct(fake_slurm: Path) -> None:
    jobs = echo.map(range(20))
    scheduler = dawgz.schedule(*jobs, dawgz.array(*echo.map(range(5))), backend="slurm")

    view = store.Workflow.open(scheduler.path)
    before = sacct_calls(fake_slurm)

    assert sacct.refresh([view], ttl=0) == 1
    assert sacct_calls(fake_slurm) == before + 1

    # Within the TTL, the cache is used
    assert sacct.refresh([store.Workflow.open(scheduler.path)], ttl=60) == 0
    assert sacct_calls(fake_slurm) == before + 1


def test_cancel(fake_slurm: Path) -> None:
    @dawgz.job
    def f() -> None:
        pass

    job = f()

    scheduler = dawgz.schedule(job, backend="slurm")
    assert scheduler.state(job) == "PENDING"

    message = scheduler.cancel(job)
    assert "Terminating" in message
    assert scheduler.state(job) == "CANCELLED"

    # Nothing left to cancel
    assert scheduler.cancel() == "nothing to cancel"


def test_sacct_parse() -> None:
    text = "\n".join([
        "100|COMPLETED|None|2026-01-01T10:00:00|2026-01-01T10:01:00|00:01:00|0:0|node1|01:00:00",
        "101_[3-5,8%2]|PENDING|JobArrayTaskLimit|Unknown|Unknown|00:00:00|0:0|None assigned|UNLIMITED",
        "101_0|RUNNING|None|2026-01-01T10:00:00|Unknown|1-00:00:01|0:0|node2|1-00:00:00",
        "102|CANCELLED by 1234|None|Unknown|Unknown|00:00:00|0:0|None assigned|10:00",
        "102.batch|CANCELLED|None|Unknown|Unknown|00:00:00|0:0|None assigned|",
    ])

    entries = sacct.parse(text)

    assert entries["100"]["state"] == "COMPLETED"
    assert entries["100"]["elapsed"] == 60
    assert entries["100"]["limit"] == 3600
    assert entries["100"]["node"] == "node1"
    assert {f"101_{i}" for i in (3, 4, 5, 8)} <= set(entries)
    assert entries["101_4"]["reason"] == "JobArrayTaskLimit"
    assert entries["101_0"]["elapsed"] == 86401
    assert entries["102"]["state"] == "CANCELLED"
    assert "102.batch" not in entries
    assert "node" not in entries["102"]


def test_sacct_expand() -> None:
    assert sacct.expand("0-3") == [0, 1, 2, 3]
    assert sacct.expand("0-9:3%2") == [0, 3, 6, 9]
    assert sacct.expand("1,4-5") == [1, 4, 5]


def test_pack_early_failure_logs(slurm_exec: None, wait_slurm: Callable) -> None:
    # The job environment fails before the dawgz runtime starts
    jobs = [
        dawgz.job(lambda: None, name="x", env=["echo 'module: not found' >&2", "exit 3"])()
        for _ in range(2)
    ]
    scheduler = dawgz.schedule(*jobs, backend="slurm", quiet=True)

    wait_slurm()

    from dawgz.cli import log_path

    # No run file: the state comes from Slurm
    view = store.Workflow.open(scheduler.path)
    assert sacct.refresh([view], force=True) == 1
    assert view.summary(view.jobs[1])["state"] == "FAILED"
    assert "module: not found" in log_path(view, view.jobs[1], None).read_text()
