"""Measures for the ML features. Each returns the numbers and how many cases they rest on; none is a quality claim
on its own, and nothing here has been run against production data unless a recorded evaluation says so.

Ranking:     recall_at_k, mrr, ndcg_at_k over judged relevance
Citations:   correctness (cited passages that were eligible and supplied) and coverage (answers that cite one
             when one was eligible)
Similarity:  ranking measures plus how often a hard negative (similar symptoms, different cause) outranks the
             true match
Anomalies:   unusual windows per cohort per day, incident recall within a tolerance, detection delay
Telemetry evaluation is chronological: the model is fitted before the cutoff and judged after it.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from typing import Any, Iterable


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0


def mrr(ranked: list[str], relevant: set[str]) -> float:
    return next((1.0 / (i + 1) for i, x in enumerate(ranked) if x in relevant), 0.0)


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, x in enumerate(ranked[:k]) if x in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return dcg / ideal if ideal else 0.0


def ranking(cases: list[dict], k: int = 5) -> dict[str, Any]:
    """cases: {"ranked": [...], "relevant": [...], "hard_negatives": [...]}."""
    if not cases:
        return {"cases": 0, "note": "no judged cases: nothing to report"}
    r = [recall_at_k(c["ranked"], set(c["relevant"]), k) for c in cases]
    m = [mrr(c["ranked"], set(c["relevant"])) for c in cases]
    n = [ndcg_at_k(c["ranked"], set(c["relevant"]), k) for c in cases]
    inverted = 0
    judged_neg = 0
    for c in cases:
        negs = set(c.get("hard_negatives") or [])
        if not negs:
            continue
        judged_neg += 1
        first_rel = next((i for i, x in enumerate(c["ranked"]) if x in set(c["relevant"])), None)
        first_neg = next((i for i, x in enumerate(c["ranked"]) if x in negs), None)
        if first_neg is not None and (first_rel is None or first_neg < first_rel):
            inverted += 1
    return {"cases": len(cases), f"recall@{k}": round(statistics.mean(r), 4), "mrr": round(statistics.mean(m), 4), f"ndcg@{k}": round(statistics.mean(n), 4),
            "hard_negative_cases": judged_neg, "hard_negative_above_relevant": round(inverted / judged_neg, 4) if judged_neg else None}


def citations(answers: Iterable[dict]) -> dict[str, Any]:
    """answers: the `knowledge` section of reports. Correctness: of the passages credited as cited, the share that
    were eligible and (for a model's answer) supplied. Coverage: of answers with an eligible passage, the share
    citing at least one."""
    cited = valid = with_eligible = covered = n = 0
    for k in answers:
        n += 1
        ps = k.get("passages") or []
        eligible = [p for p in ps if p.get("authority") in ("target", "dependency", "shared", "unverified")]
        c = [p for p in ps if p.get("cited")]
        cited += len(c)
        valid += sum(1 for p in c if p in eligible and (p.get("provided") or p.get("cited_by") in ("rules", "moks")))
        if eligible:
            with_eligible += 1
            covered += bool(c)
    return {"answers": n, "cited_passages": cited, "correctness": round(valid / cited, 4) if cited else None,
            "answers_with_eligible_passages": with_eligible, "coverage": round(covered / with_eligible, 4) if with_eligible else None}


def flags_per_day(observations: list[dict], days: float) -> dict[str, float]:
    """Unusual windows per cohort per day. Without judgments these are flags, not false positives: report them
    as such until someone has judged them."""
    out: dict[str, int] = {}
    for o in observations:
        if o.get("unusual"):
            out[o["cohort"]] = out.get(o["cohort"], 0) + 1
    return {c: round(n / days, 3) for c, n in out.items()} if days > 0 else {}


def false_positives_per_day(observations: list[dict], days: float) -> dict[str, Any]:
    judged = [o for o in observations if o.get("unusual") and (o.get("feedback") or {}).get("verdict")]
    fp = [o for o in judged if o["feedback"]["verdict"] in ("false_positive", "expected")]
    per: dict[str, int] = {}
    for o in fp:
        per[o["cohort"]] = per.get(o["cohort"], 0) + 1
    return {"judged": len(judged), "unjudged_flags": sum(1 for o in observations if o.get("unusual")) - len(judged),
            "per_cohort_per_day": {c: round(n / days, 3) for c, n in per.items()} if days > 0 else {}}


def detection(observations: list[dict], incidents: list[dict], tolerance: timedelta = timedelta(hours=1)) -> dict[str, Any]:
    """incidents: {"cohort", "start": datetime}. An incident is detected when an unusual window of its cohort ends
    within [start - tolerance, start + tolerance]; the delay is that window's end minus the start."""
    if not incidents:
        return {"incidents": 0, "note": "no incidents in the evaluation period: recall cannot be measured"}
    delays, hit = [], 0
    for inc in incidents:
        ends = sorted(o["window_to"] for o in observations if o.get("unusual") and o["cohort"] == inc["cohort"] and inc["start"] - tolerance <= o["window_to"] <= inc["start"] + tolerance)
        if ends:
            hit += 1
            delays.append((ends[0] - inc["start"]).total_seconds())
    return {"incidents": len(incidents), "detected": hit, "recall": round(hit / len(incidents), 4),
            "median_delay_seconds": statistics.median(delays) if delays else None, "tolerance_seconds": tolerance.total_seconds()}


def psi(expected_props: list[float], edges: list[float], values: list[float]) -> float | None:
    """Population stability index of values against training bins. None when there is too little to compare."""
    if len(values) < 5 or not edges:
        return None
    counts = [0] * (len(edges) + 1)
    for v in values:
        counts[sum(1 for e in edges if v > e)] += 1
    actual = [c / len(values) for c in counts]
    return round(sum((a - e) * math.log((a + 1e-4) / (e + 1e-4)) for a, e in zip(actual, expected_props)), 4)


def bins(values: list[float], n: int = 10) -> tuple[list[float], list[float]]:
    """Decile edges and the training share in each bin, for psi()."""
    xs = sorted(values)
    if len(xs) < n:
        return [], []
    edges = sorted({xs[int(len(xs) * i / n)] for i in range(1, n)})
    counts = [0] * (len(edges) + 1)
    for v in xs:
        counts[sum(1 for e in edges if v > e)] += 1
    return edges, [c / len(xs) for c in counts]


def windows(start: datetime, end: datetime, minutes: int) -> list[tuple[datetime, datetime]]:
    out, cur = [], start
    while cur + timedelta(minutes=minutes) <= end:
        out.append((cur, cur + timedelta(minutes=minutes)))
        cur += timedelta(minutes=minutes)
    return out


# ---- similar incidents --------------------------------------------------------------------------------------


async def run_similar(data: dict, run_id) -> dict[str, Any]:
    """The `similar` evaluation kind: synthetic approved incidents in the evaluation realm, each case ranked by the
    production code path (similar.find) under a reader's access, then removed. Never touches operational data."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    from ..db import session_scope
    from ..models import LearningRecord
    from ..rag import access
    from ..rbac import Binding, Principal
    from . import learning, similar

    env = f"eval-similar-{str(run_id)[:8]}"
    tag = f"eval:{run_id}"
    now = datetime.now(timezone.utc)
    token = access.enter_realm(access.EVALUATION)
    keys: dict[str, str] = {}
    try:
        async with session_scope() as db:
            for inc in data["incidents"]:
                rid = uuid.uuid4()
                keys[str(rid)] = inc["id"]
                db.add(LearningRecord(id=rid, schema_version=learning.SCHEMA_VERSION, kind="incident", incident_id=None, environment=env, namespace=inc.get("namespace", "shop"),
                                      service=inc.get("service"), realm="evaluation", event_from=now - timedelta(days=inc.get("days_ago", 1)), features=inc["features"],
                                      label={"confirmed_cause": inc["cause"], "resolution": inc["resolution"], "verification": "verified", "source": tag}, label_state="approved",
                                      outcome={"resolution": "resolved"}, versions={"feature_schema": learning.FEATURE_SCHEMA}, fingerprint=uuid.uuid4().hex, content_hash=uuid.uuid4().hex))
        reader = Principal(str(uuid.uuid4()), "eval@moks.system", [Binding("viewer", env, "*")])
        cases, results = [], []
        async with session_scope() as db:
            for case in data["cases"]:
                r = await similar.find(db, reader, case["current"], environment=env, namespace=case.get("namespace", "shop"), k=data.get("k", 3))
                ranked = [keys.get(x["record_id"], x["record_id"]) for x in r["results"]]
                cases.append({"ranked": ranked, "relevant": case["relevant"], "hard_negatives": case.get("hard_negatives", [])})
                rel, negs, k = set(case["relevant"]), set(case.get("hard_negatives", [])), data.get("k", 3)
                first_rel = next((i for i, x in enumerate(ranked) if x in rel), None)
                first_neg = next((i for i, x in enumerate(ranked) if x in negs), None)
                results.append({"case": case["id"], "ranked": ranked, "relevant": case["relevant"], "hard_negatives": case.get("hard_negatives", []), "status": r["status"],
                                "score": mrr(ranked, rel), "checks": {"relevant_in_top_k": bool(set(ranked[:k]) & rel),
                                                                      "no_hard_negative_above_relevant": first_neg is None or (first_rel is not None and first_rel < first_neg)}})
    finally:
        access.leave_realm(token)
        async with session_scope() as db:
            await db.execute(delete(LearningRecord).where(LearningRecord.environment == env, LearningRecord.realm == "evaluation"))
    k = data.get("k", 3)
    metrics = ranking(cases, k)
    g = data.get("gates", {})
    gates = [{"name": f"recall@{k}", "passed": metrics[f"recall@{k}"] >= g.get("min_recall_at_k", 0.9), "value": metrics[f"recall@{k}"], "evidence": "synthetic fixture"},
             {"name": "hard_negatives", "passed": (metrics["hard_negative_above_relevant"] or 0) <= g.get("max_hard_negative_above_relevant", 0.0),
              "value": metrics["hard_negative_above_relevant"], "evidence": "synthetic fixture"}]
    return {"results": results, "score": metrics["mrr"], "gates": gates, "metrics": {**metrics, "note": data.get("description", "")}}


async def reviewed_similar(k: int = 5) -> dict[str, Any]:
    """Similar-incident retrieval measured on what operators judged: for each investigation with reviewed
    judgments, `similar` verdicts are relevant and `not_similar` ones are hard negatives, and the incidents are
    ranked again by today's code under the investigator's original access. Read-only."""
    import uuid

    from sqlalchemy import select

    from ..orchestrator.engine import load_principal
    from ..db import session_scope
    from ..models import Environment, Investigation, LearningJudgment
    from . import learning, similar

    async with session_scope() as db:
        rows = (await db.execute(select(LearningJudgment).where(LearningJudgment.target_kind == "similar_incident", LearningJudgment.state == "reviewed"))).scalars().all()
        by_inv: dict[str, dict[str, set]] = {}
        for j in rows:
            d = by_inv.setdefault(j.context_id, {"relevant": set(), "hard_negatives": set()})
            (d["relevant"] if j.verdict == "similar" else d["hard_negatives"] if j.verdict == "not_similar" else set()).add(j.target_id)
        cases = []
        for inv_id, d in by_inv.items():
            if not d["relevant"]:
                continue
            inv = await db.get(Investigation, uuid.UUID(inv_id))
            if inv is None or inv.report is None:
                continue
            env = await db.get(Environment, inv.environment_id)
            principal = await load_principal(db, inv.created_by)
            r = await similar.find(db, principal, learning.features(inv.report, inv.question), environment=env.name, namespace=None if inv.namespace == "*" else inv.namespace, k=10, exclude_incident=inv.incident_id)
            cases.append({"ranked": [x["incident_id"] for x in r["results"]], "relevant": sorted(d["relevant"]), "hard_negatives": sorted(d["hard_negatives"])})
    return {**ranking(cases, k), "source": "reviewed operator judgments"}
