"""Measures the real model on representative input, so resource settings come from numbers. Prints JSON."""
from __future__ import annotations

import json
import platform
import resource
import statistics
import sys
import time

PASSAGE = "A pod in CrashLoopBackOff is a container that keeps exiting. Read the previous logs, then check the exit code and any recent config change. "


def run(model, candidates: int, chars: int, repeats: int) -> dict:
    text = (PASSAGE * (chars // len(PASSAGE) + 1))[:chars]
    items = [(f"c{i}", f"{i} {text}") for i in range(candidates)]
    took = []
    for _ in range(repeats):
        start = time.perf_counter()
        model.score("why does my pod keep restarting after the rollout", items, time.monotonic() + 600)
        took.append((time.perf_counter() - start) * 1000)
    took.sort()
    return {"candidates": candidates, "chars": chars, "p50_ms": round(statistics.median(took), 1), "p95_ms": round(took[int(0.95 * len(took))], 1)}


def main(path: str) -> None:
    import torch

    from engine import Model

    out: dict = {"host": platform.platform(), "machine": platform.machine(), "torch": torch.__version__, "runs": []}
    for threads in (1, 2, 4):
        start = time.perf_counter()
        model = Model(path, threads, 16)
        model.load()
        loaded = time.perf_counter() - start
        start = time.perf_counter()
        model.warm_up()
        out.setdefault("startup", {})[str(threads)] = {"load_s": round(loaded, 2), "warm_up_s": round(time.perf_counter() - start, 2)}
        for candidates, chars in ((10, 800), (30, 800), (50, 800), (30, 2000), (30, 200)):
            out["runs"].append({"threads": threads, **run(model, candidates, chars, 12)})
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024
    out["peak_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // scale
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
