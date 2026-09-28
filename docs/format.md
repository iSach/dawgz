# Workflow records

`dawgz` records every workflow in a directory of the dawgz directory (`$DAWGZ_DIR`, by default `.dawgz` in the current directory). The records are plain JSON files, such that monitors (the `dawgz` CLI, `dawgz-tui` or your own scripts) never need to import job code, unpickle anything or query Slurm to know most states.

```
.dawgz/
├── workflows.csv                  registry: name, uid, date, backend, jobs, errors
└── <uid>/
    ├── workflow.json              static description, written by the scheduler
    ├── state.json                 cached states (scheduler, sacct), atomic replace
    ├── state.lock                 advisory lock for read-modify-write of state.json
    ├── <tag>[_<i>].run.json       status and progress, written by the running job
    ├── <tag>[_<i>].log            logs (carriage-return redraws collapsed)
    ├── <tag>.sh, <tag>.pkl        Slurm script and pickled job(s)
    ├── <tag>.pack.{sh,pkl,json}   packed jobs (one Slurm array for several jobs)
    ├── run.py                     entry point of Slurm jobs
    └── dump.pkl                   pickled scheduler (`dawgz.Scheduler.load`)
```

Tags are `<index:04d>_<name>`. Indices follow a topological order (dependencies first, then creation order). All JSON files are written atomically (temporary file and rename), which also updates the modification time of the directory: readers can skip unchanged directories cheaply.

## `workflow.json`

```json
{
  "format": 1,
  "uid": "brave_otter_3f2a9c1d",
  "name": "train.py",
  "backend": "slurm",
  "date": "2026-09-28T15:43:05",
  "timestamp": 1790624585.0,
  "cwd": "/home/me/project",
  "argv": ["train.py"],
  "host": "workstation",
  "user": "me",
  "pid": 1234,
  "sources": ["@dawgz.job\ndef train(lr): ..."],
  "jobs": [
    {
      "index": 0,
      "tag": "0000_train",
      "name": "train",
      "input": "train(0.001)",
      "array": null,
      "throttle": null,
      "deps": [[1, "success"]],
      "wait": "all",
      "pruned": {"satisfied": 0, "unsatisfied": 0},
      "jobid": "123",
      "settings": {"cpus": 4},
      "source": 0,
      "script": "0000_train.pack.sh",
      "stdout": "0000_train.pack_3.out"
    }
  ]
}
```

* `array` is the number of elements of a job array (`null` otherwise), `inputs` lists their inputs.
* `deps` only lists dependencies within the workflow (pruned jobs are summarized by `pruned`).
* `jobid` is the Slurm job ID. Packed jobs (independent jobs with identical settings submitted as a single array) have an array task ID, e.g. `"123_3"`, as well as `script` and `stdout` (output of the array task before the dawgz runtime starts).
* `pid` and `host` identify the scheduler process of local workflows.

## `state.json`

```json
{
  "format": 1,
  "updated": 1790624600.0,
  "source": "sacct",
  "finished": false,
  "jobs": {
    "0": {"state": "RUNNING", "start": 1790624590.0, "node": "node1", "limit": 3600},
    "1": {"state": "PENDING", "tasks": {"0": {"state": "COMPLETED", "exit": "0:0"}}}
  }
}
```

Entries are keyed by job index; array elements are in `tasks`, keyed by element index. States are Slurm states (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `TIMEOUT`, `OUT_OF_MEMORY`, ...). Optional fields: `reason`, `start`, `end`, `elapsed` and `limit` (seconds), `exit`, `node`, `error`. `updated` is the time of the last authoritative refresh. Local schedulers write this file on every transition and set `finished` when done.

## `<tag>[_<i>].run.json`

```json
{
  "format": 1,
  "state": "RUNNING",
  "start": 1790624590.0,
  "updated": 1790624612.0,
  "host": "node1",
  "pid": 4321,
  "jobid": "123",
  "status": "loading data",
  "progress": [
    {"desc": "epoch", "n": 3, "total": 10, "unit": "it", "rate": 0.2, "eta": 35.0, "postfix": "loss=0.1", "t": 1790624612.0}
  ]
}
```

Written by the job itself (`dawgz.runtime`) when it starts, at most once per second while its progress changes (`dawgz.progress`, `dawgz.Progress`, `dawgz.status` or parsed `tqdm` output), and when it ends (`COMPLETED` or `FAILED` with `error` and `trace`). Jobs killed by Slurm (timeout, out of memory, `scancel`) cannot write their final state: Slurm states take precedence.

## Resolving states

The effective state of a job (or element) is, in order of authority:

1. a terminal state of `state.json` (e.g. `TIMEOUT` reported by `sacct`);
2. `RUNNING` or a terminal state reported by the job in `*.run.json`;
3. a non-terminal state of `state.json`;
4. `PENDING`.

For Slurm workflows, pending jobs are further resolved with their dependencies: a job whose dependencies are unfinished is waiting (`reason: "Dependency"`), and a job whose dependencies can never be satisfied is `CANCELLED` (dawgz submits dependents with `--kill-on-invalid-dep=yes`).

Monitors only query Slurm for jobs whose state cannot be resolved from files, with a single batched `sacct` call for all workflows, at most once per `DAWGZ_SACCT_TTL` seconds (20 by default for the CLI, 30 for the TUI). The results are merged into `state.json` under `state.lock`, such that all monitors share them.
