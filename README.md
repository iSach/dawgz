# Directed Acyclic Workflow Graph Scheduling

Would you like fully reproducible and reusable experiments that run on HPC clusters as seamlessly as on your machine? Do you have to comment out large parts of your pipelines whenever something failed? Tired of writing and submitting [Slurm](https://en.wikipedia.org/wiki/Slurm_Workload_Manager) scripts? Then `dawgz` is made for you!

Experiments, pipelines, data streams and so on relate to the general concept of workflow, i.e. a set of jobs/tasks whose execution must satisfy some ordering constraints. Workflows implicitely define [directed acyclic graphs](https://en.wikipedia.org/wiki/Directed_acyclic_graph) (DAGs) whose nodes are jobs and edges are their dependencies. Using a graph representation is particularly useful for scheduling and executing the jobs of a workflow while complying to the ordering constraints.

The `dawgz` package provides a lightweight and intuitive interface to declare jobs along with their dependencies, requirements, resources, etc. Then, a single line of code is needed to execute automatically all or part of the workflow, without worrying about dependencies. Importantly, `dawgz` can also hand over the execution to resource management backends like [Slurm](https://en.wikipedia.org/wiki/Slurm_Workload_Manager), which enables to execute the same workflow on your machine and HPC clusters.

## Installation

The `dawgz` package is available on [PyPi](https://pypi.org/project/dawgz/), which means it is installable via `pip`.

```
$ pip install dawgz
```

Alternatively, if you need the latest features, you can install it using

```
$ pip install git+https://github.com/francois-rozet/dawgz
```

## Getting started

In `dawgz`, a job is a Python function decorated by `@dawgz.job`. This decorator allows to define the job's parameters, like its name, whether it is a job array, the resources it needs, etc. The job's dependencies are declared with the `@dawgz.after` decorator. At last, the `dawgz.schedule` function takes care of scheduling the jobs and their dependencies, with a selected backend. These are the core components of `dawgz`'s interface, although additional features are provided.

Follows a small example demonstrating how one could use `dawgz` to calculate `π` (very roughly) using the [Monte Carlo method](https://en.wikipedia.org/wiki/Monte_Carlo_method). We define two jobs: `generate` and `estimate`. The former is a *job array*, meaning that it is executed concurrently for all values of `i = 0` up to `tasks - 1`. It also defines a [postcondition](https://en.wikipedia.org/wiki/Postconditions) ensuring that the file `pi_{i}.npy` exists after the job's completion. If it is not the case, the job raises an `AssertionError` at runtime. The job `estimate` has `generate` as dependency, meaning it should only start after `generate` succeeded.

```python
import glob
import numpy as np
import os

from dawgz import job, after, ensure, schedule

samples = 10000
tasks = 5

@ensure(lambda i: os.path.exists(f'pi_{i}.npy'))
@job(array=tasks, cpus=1, ram='2GB', timelimit='5:00')
def generate(i: int):
    print(f'Task {i + 1} / {tasks}')

    x = np.random.random(samples)
    y = np.random.random(samples)
    within_circle = x ** 2 + y ** 2 <= 1

    np.save(f'pi_{i}.npy', within_circle)

@after(generate)
@job(cpus=2, ram='4GB', timelimit='15:00')
def estimate():
    files = glob.glob('pi_*.npy')
    stack = np.vstack([np.load(f) for f in files])
    pi_estimate = stack.mean() * 4

    print(f'π ≈ {pi_estimate}')

schedule(estimate, backend='local')
```

Running this script with the `'local'` backend displays

```
$ python examples/pi.py
Task 1 / 5
Task 2 / 5
Task 3 / 5
Task 4 / 5
Task 5 / 5
π ≈ 3.1418666666666666
```

Alternatively, on a Slurm HPC cluster, changing the backend to `'slurm'` results in the following job queue.

```
$ squeue -u username
             JOBID PARTITION     NAME     USER ST       TIME  NODES NODELIST(REASON)
           1868832       all estimate username PD       0:00      1 (Dependency)
     1868831_[2-4]       all generate username PD       0:00      1 (Resources)
         1868831_0       all generate username  R       0:01      1 compute-xx
         1868831_1       all generate username  R       0:01      1 compute-xx
```

Check out the the [interface](#Interface) and the [examples](examples/) to discover the features of `dawgz`.

## Interface

### Decorators

The package provides five decorators:

* `@dawgz.job` registers a function as a job, with its parameters (name, array, resources, ...). It should always be the first (lowest) decorator. In the following example, `a` is a job with the name `'A'` and a time limit of one hour.

    ```python
    @job(name='A', timelimit='01:00:00')
    def a():
    ```

* `@dawgz.after` adds one or more dependencies to a job. By default, the job waits for its dependencies to complete with success. The desired status can be set to `'success'` (default), `'failure'` or `'any'`. In the following example, `b` waits for `a` to complete with `'failure'`.

    ```python
    @after(a, status='failure')
    @job
    def b():
    ```

* `@dawgz.waitfor` declares whether the job has to wait for `'all'` (default) or `'any'` of its dependencies to be satisfied before starting. In the following example, `c` waits for either `a` or `b` to complete (with success).

    ```python
    @after(a, b)
    @waitfor('any')
    @job
    def c():
    ```

* `@dawgz.require` adds a [precondition](https://en.wikipedia.org/wiki/Preconditions) to a job, *i.e.* a condition that must be `True` prior to the execution of the job. If preconditions are not satisfied, the job is never executed. In the following example, `d` requires `tmp` to be an existing directory.

    ```python
    @require(lambda: os.path.isdir('tmp'))
    @job
    def d():
    ```

* `@dawgz.ensure` adds a [postcondition](https://en.wikipedia.org/wiki/Postconditions) to a job, *i.e.* a condition that must be `True` after the execution of the job. In the following example, `e` ensures that the file `tmp/dump.log` exists.

    ```python
    @after(d)
    @ensure(lambda: os.path.exists('tmp/dump.log'))
    @job
    def e():
    ```

    > Traditionally, postconditions are only **necessary** indicators that the job completed with success. In `dawgz`, they are considered both necessary and **sufficient** indicators. Therefore, postconditions can be used to detect jobs that have already been executed and prune them out from the workflow, if requested.

### Backends

Currently, `dawgz.schedule` only supports two backends: `local` and `slurm`.

* `local` schedules locally the jobs by waiting asynchronously for dependencies to complete before executing each job. It does not take the requested resources into account.
* `slurm` submits the jobs to the Slurm workload manager by generating automatically the `sbatch` submission scripts.
