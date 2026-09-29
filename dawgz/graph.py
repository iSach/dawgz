r"""Workflow graph layout helpers shared by the CLI (and mirrored by the TUI)."""

from __future__ import annotations

from collections.abc import Sequence


def groups(jobs: Sequence[dict], minimum: int = 3) -> list[list[dict]]:
    r"""Groups consecutive sibling jobs (same name, dependencies and dependents).

    Fan-outs such as `[task(i) for i in range(50)]` then appear as a single node.
    """

    children: dict[int, list[int]] = {job["index"]: [] for job in jobs}

    for job in jobs:
        for dep, _ in job.get("deps", []):
            if dep in children:
                children[dep].append(job["index"])

    def key(job: dict) -> tuple:
        deps = tuple(sorted((d, s) for d, s in job.get("deps", [])))
        return (job["name"], bool(job.get("array")), deps, tuple(sorted(children[job["index"]])))

    out: list[list[dict]] = []

    for job in jobs:
        if out and key(out[-1][0]) == key(job) and not job.get("array"):
            out[-1].append(job)
        else:
            out.append([job])

    # Only keep large enough groups
    result = []
    for group in out:
        if len(group) >= minimum:
            result.append(group)
        else:
            result.extend([job] for job in group)

    return result


def lanes(
    nodes: Sequence[int],
    parents: dict[int, Sequence[int]],
) -> list[list[str]]:
    r"""Lays out a DAG as `git log --graph` style lanes, one row per node.

    Arguments:
        nodes: The nodes in topological order.
        parents: The parents of each node.

    Returns:
        For each node, a list of 2-character cells. The node itself is marked with `@`.
    """

    order = {n: k for k, n in enumerate(nodes)}
    children: dict[int, list[int]] = {n: [] for n in nodes}

    for n in nodes:
        for p in parents.get(n, ()):
            if p in children:
                children[p].append(n)

    for n in children:
        children[n].sort(key=order.__getitem__)

    active: list[int | None] = []
    rows = []

    for n in nodes:
        cols = [c for c, t in enumerate(active) if t == n]

        if cols:
            col = cols[0]
        else:
            col = next((c for c, t in enumerate(active) if t is None), len(active))
            if col == len(active):
                active.append(None)

        merges = cols[1:]
        free = [c for c, t in enumerate(active) if t is None and c != col]
        kids = children[n]
        splits = []

        for _ in kids[1:]:
            c = next((c for c in free if c > col and c not in splits), None)
            if c is None:
                c = len(active)
                active.append(None)
            splits.append(c)

        right = max([col, *merges, *splits])
        cells = []

        for c in range(len(active)):
            if c == col:
                ch = "@"
            elif c in merges:
                ch = "╯"
            elif c in splits:
                ch = "╮"
            elif active[c] is not None:
                ch = "┼" if col < c < right else "│"
            else:
                ch = "─" if col < c < right else " "

            cells.append(ch + ("─" if col <= c < right else " "))

        for c in merges:
            active[c] = None

        active[col] = kids[0] if kids else None

        for c, k in zip(splits, kids[1:], strict=True):
            active[c] = k

        while active and active[-1] is None:
            active.pop()

        rows.append(cells)

    return rows
