r"""Command-line interface.

    dawgz                          list workflows
    dawgz <wf>                     show the jobs of a workflow
    dawgz <wf> <job> [<i>]         show a job (or array element) and its logs
    dawgz logs <wf> <job> [<i>]    print (or follow with -f) the logs of a job
    dawgz cancel <wf> [<job> [<i>]]
    dawgz du                       disk usage of workflows
    dawgz clean                    delete old workflows or shrink their logs
    dawgz tui                      interactive terminal interface

Workflows can be referred to by index (negative indices count from the end), by name or
by (a prefix of their) ID. Jobs can be referred to by index or by name.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

from pathlib import Path

from . import store, term
from .constants import get_dawgz_dir
from .store import CANCELLED, DONE, FAILED, PENDING, RUNNING, Workflow

COMMANDS = ("ls", "list", "show", "logs", "cancel", "du", "clean", "tui", "help")


class CLIError(Exception):
    pass


# Resolution


def records(dawgz_dir: Path) -> list[tuple[dict, Path]]:
    return store.workflows(dawgz_dir)


def resolve(dawgz_dir: Path, ref: str) -> tuple[int, Workflow]:
    rows = records(dawgz_dir)

    if not rows:
        raise CLIError(f"no workflow in {dawgz_dir}")

    index = None

    try:
        k = int(ref)
    except ValueError:
        k = None

    if k is not None:
        if not -len(rows) <= k < len(rows):
            raise CLIError(f"workflow index {k} out of range (0 to {len(rows) - 1})")
        index = k % len(rows)
    else:
        for j in reversed(range(len(rows))):
            row = rows[j][0]
            if row["uid"] == ref or row["uid"].startswith(ref) or row["name"] == ref:
                index = j
                break

        if index is None:
            raise CLIError(f"no workflow matches '{ref}'")

    row, path = rows[index]
    workflow = Workflow.open(path, row)

    if workflow is None:
        raise CLIError(f"workflow '{row['uid']}' has no record (yet)")

    return index, workflow


def resolve_job(workflow: Workflow, ref: str) -> dict:
    jobs = workflow.jobs

    try:
        k = int(ref)
    except ValueError:
        k = None

    if k is not None:
        if not -len(jobs) <= k < len(jobs):
            raise CLIError(f"job index {k} out of range (0 to {len(jobs) - 1})")
        return jobs[k % len(jobs)]

    for job in jobs:
        if job["name"] == ref or job["tag"] == ref:
            return job

    raise CLIError(f"no job matches '{ref}'")


def refresh(workflows: list[Workflow], args: argparse.Namespace) -> None:
    if getattr(args, "offline", False):
        return

    from . import sacct

    try:
        sacct.refresh(workflows, force=getattr(args, "refresh", False))
    except (OSError, RuntimeError) as e:
        warn(f"could not query Slurm: {e}")


def warn(message: str) -> None:
    print(term.style(f"warning: {message}", "yellow"), file=sys.stderr)


# Rendering helpers


def counts_text(counts: dict[str, int], total: int | None = None) -> str:
    parts = []

    for cat in (DONE, RUNNING, FAILED, CANCELLED, PENDING):
        n = counts.get(cat, 0)
        if n:
            parts.append(term.style(f"{term.STATES[cat][0]}{term.count(n)}", term.STATES[cat][2]))

    return " ".join(parts)


def elapsed(entry: dict, now: float) -> float | None:
    if entry.get("elapsed") is not None and store.is_terminal(entry.get("state")):
        return entry["elapsed"]

    start = entry.get("start")
    if not start:
        return None

    end = entry.get("end") if store.is_terminal(entry.get("state")) else None
    return (end or now) - start


def bar_text(bar: dict) -> str:
    n, total = bar.get("n", 0), bar.get("total")
    parts = []

    if total:
        parts.append(f"{term.count(n)}/{term.count(total)}")
    else:
        parts.append(f"{term.count(n)} {bar.get('unit', 'it')}")

    if bar.get("rate"):
        rate = bar["rate"]
        parts.append(
            f"{rate:.3g} {bar.get('unit') or 'it'}/s"
            if rate >= 1
            else f"{1 / rate:.3g} s/{bar.get('unit') or 'it'}"
        )
    if bar.get("eta") is not None and total:
        parts.append(f"eta {term.duration(bar['eta'])}")

    return " · ".join(parts)


def progress_cell(workflow: Workflow, job: dict, summary: dict, size: int) -> str:
    total = summary["total"]
    counts = summary["counts"]
    elements = summary["elements"]

    if total > 1:
        return f"{term.state_bar(counts, size, summary['fraction'])} {counts_text(counts)}"

    entry = elements[0]
    cat = store.category(entry["state"])

    if cat == RUNNING:
        main = store.main_bar(entry)
        if main is not None:
            frac = store.progress_fraction(entry) or 0.0
            desc = main.get("desc", "")
            desc = "" if desc == "progress" else f"{desc} "
            return (
                f"{term.bar([(frac, 'cyan')], size)} {term.style(f'{100 * frac:3.0f}%', 'cyan')} "
                f"{term.style(desc + bar_text(main), 'gray')}"
            )
        if entry.get("status"):
            return term.style(entry["status"], "gray")
        return ""

    if cat == PENDING:
        reason = entry.get("reason") or ""
        deps = job.get("deps", [])
        if reason == "Dependency" and deps:
            waiting = [
                d
                for d, _ in deps
                if not all(
                    store.is_terminal(e["state"]) for e in workflow.elements(workflow.jobs[d])
                )
            ]
            if not waiting:
                return term.style("dependency", "gray")
            names = ", ".join(f"#{d}" for d in waiting[:4]) + ("…" if len(waiting) > 4 else "")
            return term.style(f"waiting for {names}", "gray")
        if reason == "DependencyNeverSatisfied":
            return term.style("dependency never satisfied", "yellow")
        if reason in ("Priority", "Resources", "JobArrayTaskLimit", "QOSMaxJobsPerUserLimit"):
            reason = {"JobArrayTaskLimit": "throttled", "QOSMaxJobsPerUserLimit": "QOS limit"}.get(
                reason, reason.lower()
            )
        return term.style(reason, "gray") if reason else ""

    if cat in (FAILED, CANCELLED):
        text = entry.get("error") or entry.get("reason") or ""
        if text == "DependencyNeverSatisfied":
            text = "dependency never satisfied"
        if text and "\n" in text:
            text = text.strip().splitlines()[-1]
        if entry["state"] not in ("FAILED", "CANCELLED"):
            text = entry["state"].lower().replace("_", " ") + (f" · {text}" if text else "")
        return term.style(text, "red" if cat == FAILED else "yellow")

    return ""


def state_cell(summary: dict) -> str:
    cat = store.category(summary["state"])
    state = summary["state"]
    text = state.lower().replace("_", " ") if state in store.CATEGORIES else state.lower()
    return term.label(cat, text)


# Commands


def cmd_list(args: argparse.Namespace) -> int:
    dawgz_dir = args.dir
    rows = records(dawgz_dir)
    offset = 0

    if not args.all and len(rows) > args.last:
        offset = len(rows) - args.last
        rows = rows[offset:]

    workflows = []
    for row, path in rows:
        workflows.append(Workflow.open(path, row))

    refresh([w for w in workflows if w is not None], args)

    if args.json:
        out = []
        for k, (row, _) in enumerate(rows):
            w = workflows[k]
            out.append({
                "index": offset + k,
                **row,
                "totals": w.totals() if w else None,
            })
        print(json.dumps(out, indent=2))
        return 0

    if not rows:
        print(term.style(f"no workflow in {dawgz_dir}", "gray"))
        return 0

    now = time.time()
    width = term.terminal_width()
    bar_size = 20 if width >= 100 else 12
    table = []
    oldest = None

    for k, (row, _) in enumerate(rows):
        w = workflows[k]

        if w is None:
            table.append([
                str(offset + k),
                row["name"],
                term.style(row["uid"], "gray"),
                "",
                row.get("backend", ""),
                term.style("submitting…", "gray"),
                row.get("jobs", ""),
            ])
            continue

        totals = w.totals()
        total = sum(totals.values())
        finished = totals[DONE] + totals[FAILED] + totals[CANCELLED]
        frac = sum(w.summary(j)["fraction"] * w.summary(j)["total"] for j in w.jobs) / max(
            total, 1
        )
        progress = f"{term.state_bar(totals, bar_size, frac)} {counts_text(totals)}"

        if w.backend == "slurm" and finished < total and w.updated:
            oldest = min(oldest or now, w.updated)

        table.append([
            term.style(str(offset + k), "gray"),
            term.style(w.name, "bold"),
            term.style(w.uid, "gray"),
            term.age(w.timestamp, now),
            w.backend,
            progress,
            str(len(w.jobs)),
        ])

    lines = term.table(
        table,
        header=["#", "WORKFLOW", "ID", "CREATED", "BACKEND", "PROGRESS", "JOBS"],
        align=">>" + "<" * 4 + ">",
        flex=2,
        max_width=width,
    )

    footer = []
    if offset:
        footer.append(f"{offset} older workflow{'s' if offset > 1 else ''} hidden (--all)")
    if oldest:
        footer.append(f"slurm states from {term.age(oldest, now)}")

    output(lines + ([term.style(" · ".join(footer), "gray")] if footer else []))

    return 0


def cmd_show(args: argparse.Namespace) -> int:
    index, workflow = resolve(args.dir, args.workflow)

    if args.job is not None:
        return cmd_job(args, index, workflow)

    refresh([workflow], args)

    if args.json:
        print(json.dumps(snapshot(workflow), indent=2))
        return 0

    output(render_workflow(index, workflow, args))

    return 0


def render_workflow(index: int, workflow: Workflow, args: argparse.Namespace) -> list[str]:
    now = time.time()
    width = term.terminal_width()
    totals = workflow.totals()
    total = sum(totals.values())
    summaries = {job["index"]: workflow.summary(job) for job in workflow.jobs}
    frac = sum(s["fraction"] * s["total"] for s in summaries.values()) / max(total, 1)

    # Header
    meta = [workflow.backend, term.age(workflow.timestamp, now), f"{len(workflow.jobs)} jobs"]
    if total != len(workflow.jobs):
        meta[-1] += f" ({total} tasks)"
    if workflow.backend == "slurm" and workflow.updated:
        meta.append(f"slurm states from {term.age(workflow.updated, now)}")

    lines = [
        f"{term.style(workflow.name, 'bold')}  {term.style(workflow.uid, 'gray')}  "
        + term.style(" · ".join(meta), "gray"),
        f"{term.state_bar(totals, min(48, width - 30), frac)} {term.style(f'{100 * frac:3.0f}%', 'bold')}  {counts_text(totals)}",
        "",
    ]

    # Rows (grouped fan-outs)
    from .graph import groups, lanes

    grouped = [[job] for job in workflow.jobs] if args.expand else groups(workflow.jobs)
    node_of = {}
    for k, group in enumerate(grouped):
        for job in group:
            node_of[job["index"]] = k

    parents = {
        k: sorted({node_of[d] for job in group for d, _ in job.get("deps", []) if d in node_of})
        for k, group in enumerate(grouped)
    }
    gutter = lanes(list(range(len(grouped))), parents)
    graph_width = max((len(r) for r in gutter), default=0)
    show_graph = 0 < graph_width <= 12 and any(parents.values())

    bar_size = 16 if width >= 110 else 10
    table = []

    for k, group in enumerate(grouped):
        if len(group) == 1:
            job = group[0]
            summary = summaries[job["index"]]
            name = job["name"] + (f"[0-{job['array'] - 1}]" if job.get("array") else "")
            ids = str(job["index"])
        else:
            job = group[0]
            summary = merge_summaries([summaries[j["index"]] for j in group])
            name = f"{job['name']} ×{len(group)}"
            ids = f"{group[0]['index']}-{group[-1]['index']}"

        cat = store.category(summary["state"])
        cell = ""
        if show_graph:
            cells = gutter[k] + ["  "] * (graph_width - len(gutter[k]))
            cell = "".join(
                term.style(c[0], term.STATES[cat][2]) + term.style(c[1], "gray")
                if c[0] == "@"
                else term.style(c, "gray")
                for c in cells
            ).replace("@", term.STATES[cat][0])
            if not term.COLOR:
                cell = cell.replace("@", term.STATES[cat][0])

        entries = summary["elements"]
        times = [t for t in (elapsed(e, now) for e in entries) if t is not None]
        jobids = sorted({j.get("jobid") for j in group if j.get("jobid")})
        jobid = jobids[0] + (f" +{len(jobids) - 1}" if len(jobids) > 1 else "") if jobids else ""

        table.append([
            term.style(ids, "gray"),
            cell,
            term.style(name, "bold") if cat == RUNNING else name,
            state_cell(summary),
            progress_cell(workflow, job, summary, bar_size),
            term.duration(max(times)) if times else "",
            term.style(jobid, "gray"),
        ])

    lines.extend(
        term.table(
            table,
            header=["#", "", "JOB", "STATE", "PROGRESS", "TIME", "JOBID"],
            align=">",
            flex=4,
            max_width=width,
        )
    )

    hints = [f"dawgz {index} <job>"]
    if any(len(g) > 1 for g in grouped):
        hints.append("--expand")
    lines.append("")
    lines.append(term.style("details: " + " · ".join(hints), "gray"))

    return lines


def merge_summaries(summaries: list[dict]) -> dict:
    counts = dict.fromkeys(summaries[0]["counts"], 0)
    elements = []
    fraction = 0.0

    for s in summaries:
        for key, value in s["counts"].items():
            counts[key] += value
        elements.extend(s["elements"])
        fraction += s["fraction"] * s["total"]

    total = sum(s["total"] for s in summaries)

    if counts[FAILED]:
        state = "FAILED" if counts[RUNNING] + counts[PENDING] == 0 else "RUNNING"
    elif counts[RUNNING]:
        state = "RUNNING"
    elif counts[PENDING]:
        state = "PENDING"
    elif counts[CANCELLED]:
        state = "CANCELLED"
    else:
        state = "COMPLETED"

    return {
        "state": state,
        "counts": counts,
        "total": total,
        "fraction": fraction / max(total, 1),
        "elements": elements,
    }


def cmd_job(args: argparse.Namespace, index: int, workflow: Workflow) -> int:
    job = resolve_job(workflow, args.job)
    i = None

    if args.i is not None:
        if not job.get("array"):
            raise CLIError(f"job #{job['index']} is not an array")
        i = int(args.i) % job["array"]

    refresh([workflow], args)

    entry_kind = args.entry or "logs"

    if args.raw:
        text = entry_text(workflow, job, i, entry_kind, raw=True)
        try:
            sys.stdout.write(text + ("\n" if text and not text.endswith("\n") else ""))
        except BrokenPipeError:
            pass
        return 0

    if args.json:
        data = {
            "job": job,
            "state": workflow.summary(job) if i is None else workflow.entry(job, i),
        }
        print(json.dumps(data, indent=2, default=str))
        return 0

    output(render_job(index, workflow, job, i, entry_kind, args))

    return 0


def render_job(
    index: int, workflow: Workflow, job: dict, i: int | None, kind: str, args: argparse.Namespace
) -> list[str]:
    now = time.time()
    width = term.terminal_width()
    summary = workflow.summary(job)
    entry = (
        summary if i is None else {**workflow.entry(job, i), "elements": [workflow.entry(job, i)]}
    )
    state = entry["state"]

    title = f"#{job['index']} {job['name']}"
    if job.get("array"):
        title += f"[{i}]" if i is not None else f"[0-{job['array'] - 1}]"

    lines = [f"{term.style(title, 'bold')}  {state_cell({'state': state})}"]
    info = []

    single = entry["elements"][0] if len(entry["elements"]) == 1 else None
    jobid = job.get("jobid")
    if jobid:
        info.append(("job id", jobid + (f"_{i}" if i is not None else "")))
    if single is not None:
        if single.get("node") or single.get("host"):
            info.append(("node", single.get("node") or single.get("host")))
        t = elapsed(single, now)
        if t is not None:
            limit = single.get("limit")
            info.append((
                "time",
                term.duration(t) + (f" / {term.duration(limit)}" if limit else ""),
            ))
        if single.get("start"):
            info.append((
                "started",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(single["start"])),
            ))
        if single.get("exit") and store.is_terminal(state) and single["exit"] != "0:0":
            info.append(("exit code", single["exit"]))
        if single.get("reason"):
            info.append(("reason", single["reason"]))
        if single.get("error"):
            info.append(("error", term.style(single["error"].strip().splitlines()[-1], "red")))
        if single.get("status"):
            info.append(("status", single["status"]))

    if job.get("deps"):
        deps = []
        for d, status in job["deps"]:
            dep = workflow.jobs[d]
            s = workflow.summary(dep)
            deps.append(
                f"{term.glyph(store.category(s['state']))} #{d} {dep['name']}"
                + (f" ({status})" if status != "success" else "")
            )
        info.append(("after", (" any of " if job.get("wait") == "any" else "") + ", ".join(deps)))

    for key, value in info:
        lines.append(f"  {term.style(key.ljust(9), 'gray')} {value}")

    # Progress
    if single is not None and single.get("progress"):
        lines.append("")
        size = min(30, width - 50)
        for bar in single["progress"]:
            frac = bar["n"] / bar["total"] if bar.get("total") else None
            text = f"  {term.style(str(bar.get('desc', '')).ljust(12), 'cyan')} "
            if frac is not None:
                text += f"{term.bar([(min(frac, 1.0), 'cyan')], size)} {100 * frac:3.0f}% "
            text += term.style(bar_text(bar), "gray")
            if bar.get("postfix"):
                text += "  " + bar["postfix"]
            lines.append(text)
    elif job.get("array") and i is None:
        lines.append("")
        lines.append(
            f"  {term.state_bar(summary['counts'], min(40, width - 30), summary['fraction'])} {counts_text(summary['counts'])}"
        )
        lines.append("")
        lines.extend(render_elements(workflow, job, args))
        lines.append("")
        lines.append(term.style(f"element details: dawgz {index} {job['index']} <i>", "gray"))
        return lines

    # Entry
    lines.append("")
    text = entry_text(workflow, job, i, kind, raw=False, lines=args.lines)

    if kind == "logs":
        header = "logs"
        logfile = log_path(workflow, job, i)
        if logfile is not None and logfile.exists():
            try:
                shown = logfile.relative_to(Path.cwd())
            except ValueError:
                shown = logfile
            header += f" · {term.size(logfile.stat().st_size)} · {shown}"
        lines.append(
            term.style(f"── {header} ", "gray")
            + term.style("─" * max(width - len(header) - 5, 0), "gray")
        )

    if text:
        lines.extend(text.splitlines())
    else:
        lines.append(term.style("(empty)", "gray"))

    return lines


def render_elements(workflow: Workflow, job: dict, args: argparse.Namespace) -> list[str]:
    now = time.time()
    rows = []
    limit = None if args.expand else 40
    elements = workflow.elements(job)
    shown = list(range(len(elements)))

    # Show interesting elements first when truncating
    if limit is not None and len(shown) > limit:
        rank = {RUNNING: 0, FAILED: 1, CANCELLED: 2, PENDING: 3, DONE: 4}
        shown = sorted(
            shown, key=lambda k: (rank.get(store.category(elements[k]["state"]), 5), k)
        )[:limit]
        shown.sort()

    for k in shown:
        e = elements[k]
        s = {"state": e["state"], "counts": {}, "total": 1, "fraction": 0, "elements": [e]}
        t = elapsed(e, now)
        rows.append([
            term.style(str(k), "gray"),
            state_cell(s),
            progress_cell(workflow, {**job, "array": None}, s, 12),
            term.duration(t) if t is not None else "",
            term.style(e.get("node") or e.get("host") or "", "gray"),
        ])

    lines = term.table(
        rows,
        header=["i", "STATE", "PROGRESS", "TIME", "NODE"],
        align=">",
        flex=2,
        max_width=term.terminal_width(),
        indent=2,
    )

    if len(shown) < len(elements):
        lines.append(
            term.style(f"  … {len(elements) - len(shown)} more elements (--expand)", "gray")
        )

    return lines


def log_path(workflow: Workflow, job: dict, i: int | None) -> Path | None:
    if job.get("array") and i is None:
        return None
    return store.log_file(workflow.path, job["tag"], i)


def entry_text(
    workflow: Workflow, job: dict, i: int | None, kind: str, raw: bool, lines: int | None = None
) -> str:
    if kind == "source":
        sources = workflow.meta.get("sources", [])
        k = job.get("source")
        source = sources[k] if isinstance(k, int) and k < len(sources) else ""
        return source if raw else term.highlight_python(source)
    elif kind == "input":
        if i is not None and job.get("inputs"):
            return job["inputs"][i]
        return job.get("input", "")
    elif kind == "settings":
        shfile = workflow.path / f"{job['tag']}.sh"
        if shfile.exists():
            text = shfile.read_text().strip("\n")
            return text if raw else term.highlight_shell(text)
        return json.dumps(job.get("settings", {}), indent=2)
    else:
        from .logs import read_log, tail

        logfile = log_path(workflow, job, i)
        text = ""

        if logfile is not None and logfile.exists():
            text = read_log(logfile) if raw or lines == 0 else tail(logfile, lines or 20)
        else:
            error = (workflow.entry(job, i) if i is not None or not job.get("array") else {}).get(
                "error"
            )
            text = error or ""

        return text if raw else term.highlight_logs(text)


def cmd_logs(args: argparse.Namespace) -> int:
    from .logs import follow, read_log, tail

    _, workflow = resolve(args.dir, args.workflow)
    job = resolve_job(workflow, args.job)
    i = int(args.i) % job["array"] if args.i is not None and job.get("array") else None

    if job.get("array") and i is None:
        raise CLIError(f"job #{job['index']} is an array, specify an element")

    logfile = store.log_file(workflow.path, job["tag"], i)

    try:
        if args.follow:
            follow(logfile, lines=args.lines or 20, done=lambda: _finished(workflow, job, i))
        elif not logfile.exists():
            error = workflow.entry(job, i).get("error")
            print(error or term.style("(no logs yet)", "gray"))
        elif args.lines:
            print(tail(logfile, args.lines))
        else:
            sys.stdout.write(read_log(logfile) + "\n")
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        pass

    return 0


def _finished(workflow: Workflow, job: dict, i: int | None) -> bool:
    workflow.reload()
    return store.is_terminal(workflow.entry(job, i)["state"])


def cmd_cancel(args: argparse.Namespace) -> int:
    _, workflow = resolve(args.dir, args.workflow)

    index = None
    if args.job is not None:
        index = resolve_job(workflow, args.job)["index"]

    i = int(args.i) if args.i is not None else None
    message = workflow.cancel(index, i)

    if message:
        print(message)

    return 0


def dir_usage(path: Path) -> dict[str, int]:
    usage = {"logs": 0, "pickles": 0, "other": 0, "files": 0}

    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                usage["files"] += 1
                if entry.name.endswith((".log", ".log.gz")):
                    usage["logs"] += size
                elif entry.name.endswith(".pkl"):
                    usage["pickles"] += size
                else:
                    usage["other"] += size
    except OSError:
        pass

    return usage


def cmd_du(args: argparse.Namespace) -> int:
    rows = records(args.dir)
    table = []
    totals = {"logs": 0, "pickles": 0, "other": 0, "files": 0}

    for k, (row, path) in enumerate(rows):
        usage = dir_usage(path)
        for key in totals:
            totals[key] += usage[key]
        table.append((k, row, usage))

    if args.json:
        print(json.dumps([{"index": k, **row, **usage} for k, row, usage in table], indent=2))
        return 0

    table.sort(key=lambda x: -(x[2]["logs"] + x[2]["pickles"] + x[2]["other"]))
    lines = term.table(
        [
            [
                term.style(str(k), "gray"),
                row["name"],
                term.style(row["uid"], "gray"),
                term.size(sum(usage[x] for x in ("logs", "pickles", "other"))),
                term.size(usage["logs"]),
                term.size(usage["pickles"]),
                str(usage["files"]),
            ]
            for k, row, usage in table[: None if args.all else 20]
        ],
        header=["#", "WORKFLOW", "ID", "TOTAL", "LOGS", "PICKLES", "FILES"],
        align=">>" + "<" + ">>>>",
        flex=2,
        max_width=term.terminal_width(),
    )

    total = sum(totals[x] for x in ("logs", "pickles", "other"))
    lines.append("")
    lines.append(
        term.style(
            f"{len(rows)} workflows · {term.size(total)} total · {term.size(totals['logs'])} logs · "
            f"{term.size(totals['pickles'])} pickles · {totals['files']} files in {args.dir}",
            "gray",
        )
    )
    lines.append(
        term.style(
            "free space with: dawgz clean --older-than 7d | --keep 10 | --shrink-logs 1M", "gray"
        )
    )

    output(lines)

    return 0


def parse_age(text: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    text = text.strip().lower()
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text) * 86400


def parse_size(text: str) -> int:
    units = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
    text = text.strip().lower().rstrip("ib")
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(float(text))


def cmd_clean(args: argparse.Namespace) -> int:
    from .logs import shrink

    rows = records(args.dir)
    now = time.time()
    selected = []

    targets = set()
    for ref in args.workflows:
        k, _ = resolve(args.dir, ref)
        targets.add(k)

    for k, (row, path) in enumerate(rows):
        if args.workflows and k not in targets:
            continue

        w = Workflow.open(path, row)
        timestamp = w.timestamp if w else (path.stat().st_mtime if path.exists() else 0)

        if args.older_than is not None and now - timestamp < parse_age(args.older_than):
            continue
        if args.keep is not None and k >= len(rows) - args.keep:
            continue

        active = False
        if w is not None:
            totals = w.totals()
            active = totals[RUNNING] + totals[PENDING] > 0

        if args.failed and (w is None or w.totals()[FAILED] + w.totals()[CANCELLED] == 0):
            continue

        selected.append((k, row, path, active))

    if (
        not args.workflows
        and args.older_than is None
        and args.keep is None
        and not args.failed
        and not args.shrink_logs
        and not args.pickles
    ):
        raise CLIError(
            "specify workflows or a filter (--older-than, --keep, --failed), see dawgz clean -h"
        )

    skipped = [s for s in selected if s[3] and not args.force]
    selected = [s for s in selected if not s[3] or args.force]

    for k, row, _, _ in skipped:
        warn(f"skipping active workflow {k} ({row['name']}, {row['uid']}), use --force")

    if not selected:
        print("nothing to clean")
        return 0

    freed = 0

    if args.shrink_logs or args.pickles:
        limit = parse_size(args.shrink_logs) if args.shrink_logs else None
        for _, _, path, active in selected:
            with os.scandir(path) as it:
                files = list(it)
            for entry in files:
                if limit is not None and entry.name.endswith(".log"):
                    freed += 0 if args.dry_run else shrink(Path(entry.path), limit)
                    if args.dry_run:
                        freed += max(entry.stat().st_size - limit, 0)
                elif (
                    args.pickles
                    and entry.name.endswith(".pkl")
                    and entry.name != "dump.pkl"
                    and not active
                ):
                    freed += entry.stat().st_size
                    if not args.dry_run:
                        os.remove(entry.path)
        verb = "would free" if args.dry_run else "freed"
        print(
            f"{verb} {term.size(freed)} in {len(selected)} workflow{'s' if len(selected) > 1 else ''}"
        )
        return 0

    for k, row, path, _ in selected:
        usage = dir_usage(path)
        freed += usage["logs"] + usage["pickles"] + usage["other"]
        print(
            f"{'would delete' if args.dry_run else 'delete'} {k:>3}  {row['name']}  {term.style(row['uid'], 'gray')}  {term.size(usage['logs'] + usage['pickles'] + usage['other'])}"
        )

    if args.dry_run:
        print(term.style(f"would free {term.size(freed)}", "gray"))
        return 0

    if not args.yes and sys.stdin.isatty():
        answer = input(f"delete {len(selected)} workflow(s), freeing {term.size(freed)}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    uids = {row["uid"] for _, row, _, _ in selected}

    with store.locked(args.dir / "workflows.lock"):
        for _, _, path, _ in selected:
            shutil.rmtree(path, ignore_errors=True)
        remaining = [row for row in store.registry(args.dir) if row["uid"] not in uids]
        store.rewrite_registry(args.dir, remaining)

    print(f"freed {term.size(freed)}")

    return 0


def cmd_tui(args: argparse.Namespace, extra: list[str]) -> int:
    binary = find_tui()

    if binary is None:
        raise CLIError(
            "dawgz-tui is not installed. Build it with\n"
            "  cargo install --path tui   (from the dawgz repository)\n"
            "or put a `dawgz-tui` binary on your PATH."
        )

    os.execv(binary, [binary, "--dir", str(args.dir), *extra])


def find_tui() -> str | None:
    found = shutil.which("dawgz-tui")
    if found:
        return found

    here = Path(__file__).resolve().parent
    for candidate in (
        here / "bin" / "dawgz-tui",
        here.parent / "tui" / "target" / "release" / "dawgz-tui",
        here.parent / "tui" / "target" / "debug" / "dawgz-tui",
    ):
        if candidate.exists():
            return str(candidate)

    return None


def snapshot(workflow: Workflow) -> dict:
    return {
        **{k: v for k, v in workflow.meta.items() if k not in ("jobs", "sources")},
        "updated": workflow.updated,
        "totals": workflow.totals(),
        "jobs": [
            {
                **{k: v for k, v in job.items() if k != "inputs"},
                "summary": {k: v for k, v in workflow.summary(job).items() if k != "elements"},
            }
            for job in workflow.jobs
        ],
    }


def output(lines: list[str]) -> None:
    try:
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        pass


# Parsing


def common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dir", type=Path, default=None, help="dawgz directory (default: $DAWGZ_DIR or .dawgz)"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="query Slurm even if the cached states are recent"
    )
    parser.add_argument("--offline", action="store_true", help="never query Slurm")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--color", dest="color", action="store_true", default=None, help="force colors"
    )
    parser.add_argument("--no-color", dest="color", action="store_false", help="disable colors")


def show_parser(prog: str = "dawgz") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Monitor dawgz workflows.",
        epilog="commands: "
        + ", ".join(c for c in COMMANDS if c not in ("list", "help"))
        + " (dawgz <command> -h)",
    )
    parser.add_argument("workflow", nargs="?", default=None, help="workflow index, name or ID")
    parser.add_argument("job", nargs="?", default=None, help="job index or name")
    parser.add_argument("i", nargs="?", default=None, help="job array index")
    parser.add_argument(
        "--raw", action="store_true", help="print the raw entry (e.g. full logs) only"
    )
    parser.add_argument(
        "-n", "--lines", type=int, default=None, help="number of log lines (0 for all)"
    )
    parser.add_argument(
        "-e", "--expand", action="store_true", help="do not group similar jobs or elements"
    )
    parser.add_argument("-a", "--all", action="store_true", help="list all workflows")
    parser.add_argument(
        "--last", type=int, default=20, help="number of workflows listed (default: 20)"
    )
    parser.add_argument(
        "-w",
        "--watch",
        type=float,
        nargs="?",
        const=2.0,
        default=None,
        metavar="SEC",
        help="refresh the view periodically",
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument("-c", "--cancel", action="store_true", help="cancel workflow or job")
    for entry in ("source", "settings", "input", "logs"):
        group.add_argument(
            f"--{entry}", dest="entry", action="store_const", const=entry, help=f"show job {entry}"
        )

    common(parser)

    return parser


def build_parser(command: str) -> argparse.ArgumentParser:
    prog = f"dawgz {command}"

    if command == "logs":
        parser = argparse.ArgumentParser(prog=prog, description="Print the logs of a job.")
        parser.add_argument("workflow")
        parser.add_argument("job")
        parser.add_argument("i", nargs="?", default=None)
        parser.add_argument(
            "-f", "--follow", action="store_true", help="follow the logs as they grow"
        )
        parser.add_argument(
            "-n", "--lines", type=int, default=None, help="only print the last lines"
        )
    elif command == "cancel":
        parser = argparse.ArgumentParser(
            prog=prog, description="Cancel a workflow, a job or an array element."
        )
        parser.add_argument("workflow")
        parser.add_argument("job", nargs="?", default=None)
        parser.add_argument("i", nargs="?", default=None)
    elif command == "du":
        parser = argparse.ArgumentParser(
            prog=prog, description="Show the disk usage of workflows."
        )
        parser.add_argument("-a", "--all", action="store_true")
    elif command == "clean":
        parser = argparse.ArgumentParser(
            prog=prog,
            description="Delete workflows, shrink their logs or delete their pickles.",
            epilog="examples: dawgz clean --older-than 7d · dawgz clean --keep 10 · dawgz clean --shrink-logs 1M --older-than 1d",
        )
        parser.add_argument(
            "workflows", nargs="*", help="workflows to clean (default: all matching filters)"
        )
        parser.add_argument(
            "--older-than", metavar="AGE", help="only workflows older than AGE (e.g. 30m, 12h, 7d)"
        )
        parser.add_argument(
            "--keep", type=int, metavar="N", help="keep the N most recent workflows"
        )
        parser.add_argument(
            "--failed", action="store_true", help="only workflows with failed or cancelled jobs"
        )
        parser.add_argument(
            "--shrink-logs",
            metavar="SIZE",
            help="truncate logs to SIZE (head and tail) instead of deleting",
        )
        parser.add_argument(
            "--pickles",
            action="store_true",
            help="delete job pickles of finished workflows instead of deleting",
        )
        parser.add_argument(
            "--force", action="store_true", help="include workflows with pending or running jobs"
        )
        parser.add_argument(
            "-n", "--dry-run", action="store_true", help="only show what would be done"
        )
        parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    elif command == "tui":
        parser = argparse.ArgumentParser(
            prog=prog, description="Launch the interactive terminal interface."
        )
    else:
        return show_parser()

    common(parser)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv and argv[0] in COMMANDS else None

    if command == "help":
        show_parser().print_help()
        return 0

    extra = []
    if command == "tui":
        parser = build_parser("tui")
        args, extra = parser.parse_known_args(argv[1:])
    elif command in ("ls", "list"):
        parser = show_parser()
        args = parser.parse_args(argv[1:])
    elif command is not None:
        parser = build_parser(command)
        args = parser.parse_args(argv[1:])
    else:
        parser = show_parser()
        args = parser.parse_args(argv)

    args.dir = args.dir.expanduser().resolve() if args.dir else get_dawgz_dir()

    if args.color is not None:
        term.set_color(args.color)

    try:
        if command == "tui":
            return cmd_tui(args, extra)
        elif command == "logs":
            return cmd_logs(args)
        elif command == "cancel" or getattr(args, "cancel", False):
            if args.workflow is None:
                raise CLIError("specify the workflow to cancel")
            return cmd_cancel(args)
        elif command == "du":
            return cmd_du(args)
        elif command == "clean":
            return cmd_clean(args)
        elif args.watch is not None:
            return watch(args)
        elif args.workflow is None:
            return cmd_list(args)
        else:
            return cmd_show(args)
    except CLIError as e:
        print(term.style(f"error: {e}", "red"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def watch(args: argparse.Namespace) -> int:
    r"""Re-renders the view periodically. Slurm is queried at most once per TTL."""

    interval = max(args.watch, 0.5)
    args.watch = None

    try:
        while True:
            import io

            buffer = io.StringIO()
            stdout, sys.stdout = sys.stdout, buffer
            try:
                if args.workflow is None:
                    cmd_list(args)
                else:
                    cmd_show(args)
            finally:
                sys.stdout = stdout
            sys.stdout.write("\x1b[H\x1b[2J" + buffer.getvalue())
            sys.stdout.write(
                term.style(f"\nrefreshing every {interval:g}s · ctrl-c to quit", "gray") + "\n"
            )
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
