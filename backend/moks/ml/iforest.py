"""A small Isolation Forest whose trained state is plain JSON.

Liu, Ting and Zhou (2008): points that random axis-aligned splits isolate in few steps are unusual. Written here
rather than taken from scikit-learn because the artifact must be data, not code: a pickled estimator can run
anything when loaded, and these models are small (six features, a few hundred training rows per cohort).

Preprocessing is fitted on training rows only: each feature is imputed with its training median and scaled by its
training interquartile range, and the same numbers are stored in the artifact and reused at inference. A row
whose features are all missing is not scored.

The score is in (0, 1); about 0.5 is ordinary and values near 1 are easy to isolate. The threshold is the
training score at the chosen quantile. It measures how unusual a row is, not how likely a fault is.
"""
from __future__ import annotations

import math
import random
import statistics
from typing import Any

MAX_TREES = 300
MAX_DEPTH = 16


def _c(n: int) -> float:
    """Average path length of an unsuccessful search in a binary search tree of n points."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1) + 0.5772156649) - 2.0 * (n - 1) / n


def preprocess_fit(rows: list[list[float | None]]) -> dict[str, list[float]]:
    cols = list(zip(*rows)) if rows else []
    medians, scales = [], []
    for col in cols:
        xs = sorted(v for v in col if v is not None)
        if not xs:
            medians.append(0.0)
            scales.append(1.0)
            continue
        q1, q3 = xs[len(xs) // 4], xs[(3 * len(xs)) // 4]
        medians.append(statistics.median(xs))
        scales.append(max(q3 - q1, abs(statistics.median(xs)) * 0.05, 1e-6))
    return {"median": medians, "scale": scales}


def transform(pre: dict[str, list[float]], row: list[float | None]) -> list[float] | None:
    if all(v is None for v in row):
        return None
    return [((pre["median"][i] if v is None else v) - pre["median"][i]) / pre["scale"][i] for i, v in enumerate(row)]


def _grow(rows: list[list[float]], depth: int, limit: int, rng: random.Random) -> dict:
    if depth >= limit or len(rows) <= 1:
        return {"n": len(rows)}
    dims = [d for d in range(len(rows[0])) if min(r[d] for r in rows) < max(r[d] for r in rows)]
    if not dims:
        return {"n": len(rows)}
    d = rng.choice(dims)
    lo, hi = min(r[d] for r in rows), max(r[d] for r in rows)
    split = rng.uniform(lo, hi)
    return {"d": d, "s": split, "l": _grow([r for r in rows if r[d] < split], depth + 1, limit, rng), "r": _grow([r for r in rows if r[d] >= split], depth + 1, limit, rng)}


def fit(rows: list[list[float | None]], trees: int = 100, sample: int = 256, seed: int = 0, quantile: float = 0.99) -> dict[str, Any]:
    pre = preprocess_fit(rows)
    data = [x for r in rows if (x := transform(pre, r)) is not None]
    if len(data) < 16:
        raise ValueError(f"isolation forest needs at least 16 usable rows, got {len(data)}")
    rng = random.Random(seed)
    psi = min(sample, len(data))
    limit = min(MAX_DEPTH, math.ceil(math.log2(psi)))
    forest = [_grow(rng.sample(data, psi), 0, limit, rng) for _ in range(min(trees, MAX_TREES))]
    model = {"pre": pre, "trees": forest, "psi": psi, "dims": len(data[0])}
    train_scores = sorted(_score(model, x) for x in data)
    model["threshold"] = train_scores[min(len(train_scores) - 1, int(quantile * len(train_scores)))]
    model["quantile"] = quantile
    return model


def _path(node: dict, x: list[float], depth: int = 0) -> float:
    while "d" in node:
        node = node["l"] if x[node["d"]] < node["s"] else node["r"]
        depth += 1
    return depth + _c(node["n"])


def _score(model: dict, x: list[float]) -> float:
    mean = sum(_path(t, x) for t in model["trees"]) / len(model["trees"])
    return 2.0 ** (-mean / _c(model["psi"]))


def score(model: dict, row: list[float | None]) -> float | None:
    x = transform(model["pre"], row)
    return None if x is None else _score(model, x)


def validate(model: Any, dims: int) -> None:
    """Structure only, before anything is scored: a malformed artifact is refused, never guessed at."""
    if not isinstance(model, dict) or set(model) != {"pre", "trees", "psi", "dims", "threshold", "quantile"}:
        raise ValueError("isolation forest state has unexpected keys")
    if model["dims"] != dims or len(model["pre"]["median"]) != dims or len(model["pre"]["scale"]) != dims:
        raise ValueError("isolation forest was trained on a different number of features")
    if not (1 <= len(model["trees"]) <= MAX_TREES) or not (2 <= int(model["psi"]) <= 4096):
        raise ValueError("isolation forest size out of bounds")
    if not 0 < float(model["threshold"]) < 1:
        raise ValueError("isolation forest threshold out of range")
    stack = [(t, 0) for t in model["trees"]]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DEPTH + 1 or not isinstance(node, dict):
            raise ValueError("isolation forest tree is malformed")
        if "d" in node:
            if set(node) != {"d", "s", "l", "r"} or not (0 <= int(node["d"]) < dims) or not isinstance(node["s"], (int, float)):
                raise ValueError("isolation forest split is malformed")
            stack += [(node["l"], depth + 1), (node["r"], depth + 1)]
        elif set(node) != {"n"} or int(node["n"]) < 0:
            raise ValueError("isolation forest leaf is malformed")
