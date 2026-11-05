"""Seasonal statistical baseline: what each feature usually is at this hour of the week.

For every feature it keeps the median and the median absolute deviation (MAD) per hour of week, falling back to
hour of day and then to the whole history when a bucket has too few samples. A point's deviation is a robust z
score, |x - median| / (1.4826 * MAD), with a floor on the spread so a perfectly flat history does not turn a
small change into an alarm. A window is unusual when one feature's median deviation over the window passes the
threshold. Everything is fitted on training data only.

The output says which features moved and by how much, against what is typical here. It does not say why.
"""
from __future__ import annotations

import statistics
from typing import Any

from .features import FEATURES, CohortFrame, hour_of_week

MIN_BUCKET = 4
THRESHOLD = 4.0


def _stats(xs: list[float]) -> tuple[float, float] | None:
    if len(xs) < MIN_BUCKET:
        return None
    med = statistics.median(xs)
    return med, statistics.median(abs(x - med) for x in xs)


def _floor(med: float, mad: float) -> float:
    return max(1.4826 * mad, abs(med) * 0.05, 1e-6)  # spread never below 5% of the level


def fit(frames: list[CohortFrame]) -> dict[str, Any]:
    """{feature: {"week": {how: [med, mad]}, "day": {...}, "all": [med, mad] | None}} from training frames."""
    buckets: dict[str, dict[str, dict[int, list[float]]]] = {f: {"week": {}, "day": {}} for f in FEATURES}
    every: dict[str, list[float]] = {f: [] for f in FEATURES}
    for fr in frames:
        for t, row in fr.rows():
            how = hour_of_week(t)
            for f, v in zip(FEATURES, row):
                if v is None:
                    continue
                buckets[f]["week"].setdefault(how, []).append(v)
                buckets[f]["day"].setdefault(how % 24, []).append(v)
                every[f].append(v)
    out: dict[str, Any] = {}
    for f in FEATURES:
        week = {str(k): s for k, xs in buckets[f]["week"].items() if (s := _stats(xs))}
        day = {str(k): s for k, xs in buckets[f]["day"].items() if (s := _stats(xs))}
        out[f] = {"week": week, "day": day, "all": _stats(every[f]), "samples": len(every[f])}
    return out


def expected(model: dict[str, Any], feature: str, ts: int) -> tuple[float, float, str] | None:
    """(median, spread, which bucket) for this instant, or None when the feature was never seen in training."""
    m = model.get(feature) or {}
    how = hour_of_week(ts)
    for name, key in (("hour of week", str(how)), ("hour of day", str(how % 24))):
        s = m.get("week" if name == "hour of week" else "day", {}).get(key)
        if s:
            return s[0], _floor(*s), name
    if m.get("all"):
        return m["all"][0], _floor(*m["all"]), "all history"
    return None


def score(model: dict[str, Any], frame: CohortFrame, threshold: float = THRESHOLD) -> dict[str, Any]:
    """The window's deviation per feature and whether any is unusual. Missing points are skipped and counted."""
    per: dict[str, list[tuple[float, float, float]]] = {f: [] for f in FEATURES}
    unseen: set[str] = set()
    for t, row in frame.rows():
        for f, v in zip(FEATURES, row):
            if v is None:
                continue
            e = expected(model, f, t)
            if e is None:
                unseen.add(f)
                continue
            med, spread, _ = e
            per[f].append(((v - med) / spread, v, med))
    deviations = []
    for f, pts in per.items():
        if not pts:
            continue
        z = statistics.median(p[0] for p in pts)
        deviations.append({"feature": f, "z": round(z, 2), "observed": round(statistics.median(p[1] for p in pts), 6), "typical": round(statistics.median(p[2] for p in pts), 6),
                           "direction": "higher" if z > 0 else "lower", "points": len(pts)})
    deviations.sort(key=lambda d: -abs(d["z"]))
    top = abs(deviations[0]["z"]) if deviations else 0.0
    return {"score": round(top, 3), "threshold": threshold, "unusual": top >= threshold, "deviations": deviations, "unseen_features": sorted(unseen)}
