#!/usr/bin/env python
r"""A workflow that exercises most features: arrays, fan-outs, progress and failures.

Run it locally, or on Slurm:

    python examples/showcase.py
    DAWGZ_BACKEND=slurm python examples/showcase.py
"""

import random
import time

import dawgz


@dawgz.job(cpus=1, time="10:00")
def preprocess() -> None:
    dawgz.status("downloading data")
    time.sleep(1)
    for _ in dawgz.progress(range(20), desc="preprocess"):
        time.sleep(0.05)


@dawgz.job(cpus=4, gpus=1, time="1:00:00")
def train(seed: int) -> None:
    random.seed(seed)
    epochs = 5
    for epoch in range(epochs):
        with dawgz.Progress(total=50, desc=f"epoch {epoch + 1}/{epochs}") as bar:
            for step in range(50):
                time.sleep(0.02 + 0.03 * random.random())
                bar.update(1, loss=1 / (1 + epoch + step / 50))


@dawgz.job(cpus=1, time="5:00")
def evaluate(seed: int, split: int) -> None:
    time.sleep(0.5 + random.random())
    if seed == 3 and split == 7:
        raise ValueError("NaN in predictions")
    print(f"seed={seed} split={split} acc={0.9 + 0.01 * random.random():.3f}")


@dawgz.job(cpus=1, time="5:00")
def report() -> None:
    print("all evaluations done")


@dawgz.job(cpus=1, time="5:00")
def notify() -> None:
    print("some evaluation failed")


if __name__ == "__main__":
    prep = preprocess()
    models = train.map(range(8), array=True, throttle=4).after(prep)
    evals = [evaluate(seed, split).after(models) for seed in range(4) for split in range(10)]
    done = report().after(*evals)
    alert = notify().after(*evals, status="any")

    dawgz.schedule(done, alert, workers=4)
