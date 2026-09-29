# Examples

## Simple example

In [`simple.py`](simple.py) we define a workflow composed of 5 jobs. In summary,

* `a` and `b` are concurrent.
* `c(i)` waits for `b` to complete with success.
* `c(i)` has already completed with success for `i != 42`.
* `d` waits for all `c(i)` to complete.
* `e` waits for `'any'` of its dependencies (either `a` or `d`) to complete.

The workflow graph of `e` is scheduled by `dawgz.schedule` (with 4 local workers), while pruning the jobs that have completed. We get the following output, where we notice that `e` finishes before `a`, despite its failure, reported at the end.

```
$ python examples/simple.py
▶ [1/5] a()
▶ [2/5] b()
a
b
b
✔ b · 0:02
▶ [3/5] c(42)
c42
✔ c · 0:00
▶ [4/5] d()
d
✔ d · 0:00
▶ [5/5] e()
e
✔ e · 0:00
a
Traceback (most recent call last):
  File ".../examples/simple.py", line 13, in a
    raise RuntimeError("foo")
RuntimeError: foo
✘ a · 0:03 RuntimeError: foo
✘ #0 a
  │ Traceback (most recent call last):
  │   File ".../examples/simple.py", line 13, in a
  │     raise RuntimeError("foo")
  │ RuntimeError: foo
  │
  │ JobFailedError: a()
✘ simple.py (gentle_saffron_7fc04ba0) ran 5 jobs, 1 failed · inspect with dawgz 0
$ dawgz 0
simple.py  gentle_saffron_7fc04ba0  async · 3s ago · 5 jobs
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%  ✔4 ✘1

#        JOB  STATE        PROGRESS           TIME
0  ✘     a    ✘ failed     RuntimeError: foo  0:03
1  │ ✔   b    ✔ completed                     0:02
2  │ ✔   c    ✔ completed                     0:00
3  │ ✔   d    ✔ completed                     0:00
4  ✔─╯   e    ✔ completed                     0:00
```

## Train example

In [`train.py`](train.py) we define a workflow that alternates between training and evaluation steps. The training steps are consecutive, meaning that the `i`th is always executed after the `i-1`th and before the `i+1`th. However, the evaluation steps can be executed directly after their respective training step, even though preceding evaluation steps have not completed yet. The workflow graph looks like

```
preprocessing → train_1 → train_2 → train_3
                   ↓         ↓         ↓
                 eval_1    eval_2    eval_3
```

With a single worker (the default of the `local` backend), jobs run one at a time, in the order listed by `dawgz`: dependencies first, then creation order.

```
$ python examples/train.py  # with workers=1
▶ [1/7] preprocessing()
data preprocessing
✔ preprocessing · 0:00
▶ [2/7] train(1)
training step 1
✔ train · 0:00
▶ [3/7] evaluate(1)
evaluation step 1
✔ evaluate · 0:00
▶ [4/7] train(2)
...
```

If we change the backend to `'dummy'`, we observe that the evaluation steps are not necessarily consecutive.

```
$ python examples/train.py  # with backend="dummy", workers=4
START preprocessing()
END   preprocessing()
START train(1)
END   train(1)
START evaluate(1)
START train(2)
END   train(2)
START evaluate(2)
START train(3)
END   evaluate(1)
END   train(3)
START evaluate(3)
END   evaluate(2)
END   evaluate(3)
```

## Showcase

[`showcase.py`](showcase.py) exercises most features: a job array throttled to 4 simultaneous trainings with per-epoch progress bars and metrics (`dawgz.Progress`), a fan-out of 40 evaluations (packed into a single Slurm array), a failing evaluation, and two final jobs waiting for all evaluations to succeed or to finish.

```
python examples/showcase.py                        # locally, 4 workers
DAWGZ_BACKEND=slurm python examples/showcase.py    # on Slurm
dawgz tui
```

To try it without a cluster, [`tools/demo.py`](../tools/demo.py) runs it (and a few other workflows) on a fake Slurm that executes jobs locally.

```
python tools/demo.py /tmp/demo
source /tmp/demo/env.sh  # or env.fish
dawgz tui
```
