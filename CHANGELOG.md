# Changelog

## 3.0.0 (unreleased)

### Highlights

* **Much faster submission and monitoring.** Independent `sbatch` calls run concurrently, and independent jobs with identical settings and dependencies are packed into a single job array (one task per job, with per-job logs, states and dependencies). Monitors read lightweight JSON records instead of unpickling workflows, and query Slurm with a single batched and cached `sacct` call. With 50 jobs and a 50 ms Slurm latency: submission 4.18 s → 0.18 s, `dawgz <wf>` 3.89 s → 0.13 s (cold) and 0.05 s (warm), 50 → 1 → 0 `sacct` calls.
* **Progress reporting.** `dawgz.progress`, `dawgz.Progress` and `dawgz.status` report live progress from jobs, and `tqdm` bars in job outputs are recognized automatically.
* **Compact logs and records.** Redrawn lines (e.g. `tqdm` bars, nested or not) are collapsed before reaching log files. Job functions (and the global variables they capture, e.g. a model) are stored once per workflow instead of once per job: 50 jobs using a global 16 MB model took 764 MB, now 16 MB, and a `LargeJobWarning` names the culprits. `dawgz du` and `dawgz clean` show and free disk usage.
* **New CLI** without third-party dependencies: dependency graph, grouped fan-outs with stacked progress bars, job details with progress and log tails, `logs -f`, `cancel`, `du`, `clean`, `--json`, `--watch`, references by index, name or ID.
* **`dawgz-tui`**, an interactive terminal interface written in Rust (ratatui), with workflows, jobs, arrays, timeline, graph, logs, search, filters, cancellation and a Slurm queue view.
* **Sequential local execution by default.** The new `local` backend runs each job in a fresh process, one at a time (`workers=1`) in the order listed by `dawgz`, or in parallel with `workers=N`.
* **Code snapshots.** `snapshot=True` (or `DAWGZ_SNAPSHOT=1`) copies the local code imported by the script at submission, such that Slurm jobs run with it even if it is edited before they start.
* **Friendlier interface**: `backend` defaults to `$DAWGZ_BACKEND` or `"local"`, options of other backends are ignored, `factory.map(...)`, `factory.options(...)`, `a >> b`, `throttle=` for Slurm packs.

### Bug fixes

* `@dawgz.job(name=..., env=..., shell=..., interpreter=..., settings=...)` silently dropped these options when used as a decorator with arguments.
* `False` settings were emitted as `--key=False` and `None` settings as `--key=None` in `sbatch` scripts.
* `dawgz.schedule` crashed without a `name` when `__main__` has no `__file__` (REPL, notebooks).
* Jobs with `waitfor("any")` and no dependencies never ran.
* Local workflows were only recorded after all their jobs finished, so they could not be monitored, and interrupted Slurm submissions left untracked jobs.
* Job indices depended on the order in which submissions completed; they now follow a deterministic topological order.
* On Slurm, jobs with `waitfor("any")` already satisfied by a pruned dependency still waited for their other dependencies, jobs with `waitfor("any")` were cancelled as soon as one dependency failed to submit, and exception messages could end up in `--dependency`.
* Functions imported from local modules of the submission directory could not be unpickled on compute nodes.
* Dependents of failed jobs stayed pending forever on Slurm; they are now submitted with `--kill-on-invalid-dep=yes`.
* `sys.exit()` in a job killed the whole local scheduler, and one crashing job (e.g. segmentation fault) broke all the remaining ones.
* Job outputs written at the file descriptor level (C extensions, subprocesses) were missing from local logs, and jobs using `sys.stdout.buffer` or reassigning `sys.stdout` failed or were reported as failed.
* Array elements of local workflows all reported the state of the array, and dependents of arrays could start before all elements finished.
* Scheduling the same jobs twice raised a `KeyError`, and creating jobs from functions without source code (e.g. `exec`) raised an `OSError`.
* `cancel` crashed for jobs that failed to submit, appended array indices to non-array jobs, and raised on `scancel` errors.
* `sacct` states such as `CANCELLED by 1234` were not normalized, and one `sacct` call was made per job for every view.
* Paths with quotes, spaces, braces or `%` broke the generated scripts.
* Validation relied on `assert`, which is disabled by `python -O`.
* Submission errors were displayed as `CANCELLED` instead of `FAILED`.
* Invalid CLI references raised raw tracebacks; they now print an error and exit with code 2.
* Generating workflow IDs took about 0.2 s (and could produce odd words).
* Every job pickle embedded a copy of the global variables used by its function.

### Changes

* `rich` and `wonderwords` are no longer dependencies. `import dawgz` imports no third-party package until jobs are created.
* The `async` backend is an alias of `local` (one process per job instead of a process pool). Its `max_workers` argument is still accepted.
* On Slurm, identical independent jobs appear as tasks of a job array in `squeue` (disable with `pack=False` or `DAWGZ_PACK=0`).
* Workflows are recorded in `workflow.json`, `state.json` and `*.run.json` files ([format](docs/format.md)). Workflows recorded by previous versions are migrated automatically the first time they are displayed.
* `Scheduler.report` and `Scheduler.lookup` (rich renderables used by the old CLI) were removed.
