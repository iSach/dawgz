# dawgz-tui

Interactive terminal interface for [dawgz](../README.md) workflows, built with [ratatui](https://ratatui.rs).

```
cargo install --path .     # or: cargo build --release
dawgz tui                  # or: dawgz-tui [--dir DIR] [--all] [--interval SECS] [--offline] [--theme mocha|latte|terminal]
```

It reads the records written by dawgz ([format](../docs/format.md)) directly, without Python: files of the selected workflow are checked every second, those of other active workflows every 5 seconds, and only changed files are read again. Slurm is queried in a background thread, with a single batched `sacct` call for the jobs whose state cannot be known from files, at most once per interval (30 s by default, never less than 5 s). Results are written back to `state.json`, where the `dawgz` CLI reuses them.

| Key | |
|---|---|
| `↑↓` `jk` | select |
| `←→` `hl` | focus sidebar / main, collapse / expand fan-outs and arrays |
| `⏎` | open (expand, logs, jump from the graph to the job) |
| `1`-`4`, `tab` | jobs, graph, logs, info |
| `/` | fuzzy search workflows (sidebar) or jobs (main) |
| `a`, `s`, `D` | active workflows only, job state filter, all known directories |
| `Q` | Slurm queue of all your jobs |
| `c`, `y`, `r` | cancel, copy the Slurm job ID (OSC 52), query Slurm now |
| `f`, `w`, `g`/`G` | logs: follow, wrap, top/bottom |
| `?`, `q` | help, quit |

`--snapshot WxH` renders a single frame to standard output (with ANSI colors) and exits, which is used by the tests.

## Layout of the code

* `model.rs`: records (`workflow.json`, `state.json`, `*.run.json`) and state categories
* `store.rs`: loading with change detection, state merging and dependency inference (mirrors `dawgz/store.py`)
* `sacct.rs`: batched `sacct` and `squeue` queries
* `graph.rs`: fan-out groups, `git log --graph` lanes and the layered DAG layout
* `logs.rs`: bounded log reading, carriage returns and ANSI colors
* `app.rs`: state, background refresh and input handling
* `ui.rs`, `widgets.rs`, `theme.rs`: rendering
