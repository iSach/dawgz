r"""Batched Slurm accounting queries.

A single `sacct` call fetches the state of every requested job, which keeps the load on
the Slurm database constant regardless of the size of workflows. Results are cached in
each workflow's `state.json` by `dawgz.store.Workflow.update`.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from datetime import datetime

FIELDS = (
    "JobID",
    "State",
    "Reason",
    "Start",
    "End",
    "Elapsed",
    "ExitCode",
    "NodeList",
    "Timelimit",
)
MINIMAL = ("JobID", "State")
CHUNK = 500

# Seconds between two refreshes of the same workflow
TTL = float(os.environ.get("DAWGZ_SACCT_TTL", 20))

JOBID = re.compile(r"^(\d+)(?:_(\d+|\[[^\]]*\]))?$")


def query(jobids: list[str]) -> dict[str, dict]:
    r"""Queries the states of jobs (and their array elements) with as few calls as possible.

    Returns:
        A mapping from job ID (e.g. `"123"` or `"123_4"` for array elements) to entry.
    """

    entries = {}

    # Array tasks (e.g. packed jobs "123_4") are fetched with their array ("123")
    jobids = list(dict.fromkeys(jobid.split("_")[0] for jobid in jobids))

    for k in range(0, len(jobids), CHUNK):
        entries.update(_query(jobids[k : k + CHUNK]))

    return entries


def _query(jobids: list[str]) -> dict[str, dict]:
    if not jobids:
        return {}

    for fields in (FIELDS, MINIMAL):
        proc = subprocess.run(
            ["sacct", "-X", "-n", "-P", "-j", ",".join(jobids), "-o", ",".join(fields)],
            capture_output=True,
            text=True,
        )

        if proc.returncode == 0:
            return parse(proc.stdout, fields)

    raise RuntimeError(proc.stderr.strip() or "sacct failed")


def parse(text: str, fields: tuple[str, ...] = FIELDS) -> dict[str, dict]:
    entries = {}

    for line in text.splitlines():
        values = line.split("|")

        if len(values) < len(fields):
            continue

        row = dict(zip(fields, values, strict=False))
        match = JOBID.match(row["JobID"].strip())

        if match is None:  # job steps, heterogeneous jobs, ...
            continue

        jobid, task = match.groups()
        entry = convert(row)

        if task is None:
            entries[jobid] = entry
        elif task.startswith("["):
            for i in expand(task[1:-1]):
                entries[f"{jobid}_{i}"] = dict(entry)
            entries.setdefault(jobid, dict(entry))
        else:
            entries[f"{jobid}_{task}"] = entry

    return entries


def expand(ranges: str) -> list[int]:
    r"""Expands a Slurm array range such as `0-9,12,15-20:2%4`."""

    indices = []

    for part in ranges.split("%")[0].split(","):
        if not part:
            continue

        step = 1
        if ":" in part:
            part, step = part.split(":")
            step = int(step)

        if "-" in part:
            a, b = part.split("-")
            indices.extend(range(int(a), int(b) + 1, step))
        else:
            indices.append(int(part))

    return indices


def convert(row: dict[str, str]) -> dict:
    # "CANCELLED by 1234" -> "CANCELLED"
    state = row.get("State", "UNKNOWN").split()[0].rstrip("+") if row.get("State") else "UNKNOWN"
    entry = {"state": state}

    if row.get("Reason") and row["Reason"] not in ("None", ""):
        entry["reason"] = row["Reason"]
    if "Start" in row:
        entry["start"] = timestamp(row["Start"])
    if "End" in row:
        entry["end"] = timestamp(row["End"])
    if row.get("Elapsed"):
        entry["elapsed"] = duration(row["Elapsed"])
    if row.get("ExitCode"):
        entry["exit"] = row["ExitCode"]
    if row.get("NodeList") and not row["NodeList"].startswith("None"):
        entry["node"] = row["NodeList"]
    if row.get("Timelimit"):
        entry["limit"] = duration(row["Timelimit"])

    return {k: v for k, v in entry.items() if v is not None}


def timestamp(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def duration(text: str) -> float | None:
    r"""Parses Slurm durations such as `1-02:03:04`, `02:03:04`, `03:04` or `03:04.123`."""

    try:
        days, _, rest = text.rpartition("-")
        parts = [float(p) for p in rest.split(":")]
        seconds = 0.0
        for p in parts:
            seconds = 60 * seconds + p
        return seconds + 86400 * (int(days) if days else 0)
    except ValueError:
        return None


def refresh(records: list, ttl: float = TTL, force: bool = False) -> int:
    r"""Refreshes stale Slurm workflows with a single `sacct` call.

    Returns:
        The number of `sacct` calls (0 or more for very large workflows).
    """

    stale = {}

    for record in records:
        jobs = record.stale(0.0 if force else ttl)
        if jobs:
            stale[record] = jobs

    if not stale:
        return 0

    jobids = list(
        dict.fromkeys(job["jobid"].split("_")[0] for jobs in stale.values() for job in jobs)
    )
    now = time.time()
    entries = query(jobids)

    for record in stale:
        record.update(entries, now)

    return (len(jobids) + CHUNK - 1) // CHUNK
