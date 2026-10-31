"""Enterprise evaluation gates. Policy and safety are decided by deterministic assertions, never by a
model judging a model. A gate passes or fails; the score only summarizes quality.

rag    real ingestion and ranking in PostgreSQL over synthetic documents: recall@k, MRR, exact
       identifiers, access isolation, abstention, freshness, injection, latency and cost.
skills selection, rejection, planned tools and arguments, disabled and unavailable skills, publication
       validation, declared evaluation cases, detection identity and recovery.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any

from sqlalchemy import select

from .. import detection, skills
from ..db import session_scope
from ..models import Document, KnowledgeSource
from ..orchestrator import prompts
from ..rag import ingest, service
from ..rag.chunking import PIPELINE_VERSION
from ..rbac import Binding, Principal
from ..skills import readiness as rd
from ..skills import registry
from ..skills.validate import errors, validate


def _gate(name: str, failures: list[str], total: int) -> dict:
    return {"name": name, "passed": not failures, "checked": total, "failures": failures[:20]}


def versions(dataset: dict) -> dict:
    lib = skills.library()
    return {"dataset": hashlib.sha256(json.dumps(dataset, sort_keys=True).encode()).hexdigest()[:16], "skills_generation": lib.generation, "skills_digest": lib.digest,
            "pipeline": PIPELINE_VERSION, "prompt": hashlib.sha256((prompts.PLANNER_SYSTEM + prompts.REPORT_SYSTEM).encode()).hexdigest()[:16]}


def _ndcg(ranked: list[str], relevant: set[str]) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, d in enumerate(ranked) if d in relevant)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(relevant), len(ranked)))) or 1
    return dcg / ideal


async def run_rag(data: dict, run_id: uuid.UUID) -> dict[str, Any]:
    tag = str(run_id)[:8]
    env = lambda e: f"eval-{tag}-{e}"  # names no real binding matches, so a run cannot leak into real searches
    by_doc: dict[uuid.UUID, str] = {}
    async with session_scope() as db:
        sources: dict[tuple, KnowledgeSource] = {}
        for d in data["documents"]:
            key = (d["environment"], d["namespace"])
            if key not in sources:
                sources[key] = KnowledgeSource(name=f"eval-{tag}-{len(sources)}", type="upload", config={}, scope_environment=env(key[0]), scope_namespace=key[1], classification="internal")
                db.add(sources[key])
                await db.flush()
            doc = await service.index_document(db, sources[key], d["path"], d["text"], revision=tag, url=None, extra={"service": d.get("service")})
            by_doc[doc.id] = d["id"]
    await service.drain_outbox(len(by_doc) * 2)
    people = {name: Principal(f"00000000-0000-0000-0000-{i:012d}", f"{name}@eval", [Binding(r, env(e) if e != "*" else "*", n) for r, e, n in rows])
              for i, (name, rows) in enumerate(data["bindings"].items())}
    results, recall, mrr, ndcg, latency = [], [], [], [], []
    failures: dict[str, list[str]] = {"isolation": [], "exact_identifier": [], "abstention": [], "freshness": [], "injection": []}
    checked = dict.fromkeys(failures, 0)
    before = {n: s.digest for n, s in skills.library().skills.items()}
    for case in data["cases"]:
        start = time.monotonic()
        async with session_scope() as db:
            r = await service.search(db, people[case["as"]], case["query"], 5, environment=env(case["environment"]) if case.get("environment") else None)
        latency.append(time.monotonic() - start)
        ranked = [by_doc.get(uuid.UUID(h["document_id"]), "?") for h in r["results"]]
        ranked = [x for x in ranked if x != "?"]
        relevant = set(case.get("relevant") or [])
        row: dict[str, Any] = {"case": case["id"], "ranked": ranked, "mode": r["mode"], "answer": r["evidence"]["state"], "checks": {}}
        if relevant:
            hit = len(relevant & set(ranked)) / len(relevant)
            recall.append(hit)
            mrr.append(next((1 / (i + 1) for i, d in enumerate(ranked) if d in relevant), 0))
            ndcg.append(_ndcg(ranked, relevant))
            row["checks"]["recall"] = hit == 1
        if case.get("forbidden"):
            checked["isolation"] += 1
            leak = set(case["forbidden"]) & set(ranked)
            row["checks"]["isolation"] = not leak
            if leak:
                failures["isolation"].append(f"{case['id']}: {sorted(leak)}")
        if case.get("exact"):
            checked["exact_identifier"] += 1
            ok = ranked[:1] == case["relevant"]
            row["checks"]["exact_identifier"] = ok
            if not ok:
                failures["exact_identifier"].append(f"{case['id']}: {ranked[:3]}")
        if case.get("answer"):
            checked["abstention"] += 1
            ok = r["evidence"]["state"] in case["answer"]
            row["checks"]["abstention"] = ok
            if not ok:
                failures["abstention"].append(f"{case['id']}: {r['evidence']['state']}")
        if case.get("first"):
            checked["freshness"] += 1
            states = {by_doc.get(uuid.UUID(h["document_id"])): h["freshness"]["state"] for h in r["results"]}
            ok = ranked[:1] == [case["first"]] and all(states.get(k) == v for k, v in (case.get("flagged") or {}).items())
            row["checks"]["freshness"] = ok
            if not ok:
                failures["freshness"].append(f"{case['id']}: {ranked[:3]} {states}")
        if case.get("gate") == "injection":
            checked["injection"] += 1
            same = {n: s.digest for n, s in skills.library().skills.items()} == before
            ok = same and bool(relevant & set(ranked))  # retrieved as ordinary data, and nothing changed because of it
            row["checks"]["injection"] = ok
            if not ok:
                failures["injection"].append(case["id"])
        row["score"] = round(sum(row["checks"].values()) / len(row["checks"]), 4) if row["checks"] else 1.0
        results.append(row)
    async with session_scope() as db:
        for s in (await db.execute(select(KnowledgeSource).where(KnowledgeSource.name.like(f"eval-{tag}-%")))).scalars():
            for d in (await db.execute(select(Document).where(Document.source_id == s.id))).scalars():
                await service.remove_document(db, d)
            s.lifecycle = "deleted"
    await service.drain_outbox(len(by_doc) * 2)
    latency.sort()
    metrics = {"recall_at_5": _mean(recall), "mrr": _mean(mrr), "ndcg_at_5": _mean(ndcg), "latency_p50_ms": round(latency[len(latency) // 2] * 1000, 1), "latency_max_ms": round(latency[-1] * 1000, 1),
               "cost_usd": 0, "mode": results[0]["mode"] if results else None}
    gates = [_gate(k, v, checked[k]) for k, v in failures.items()]
    gates.append({"name": "recall_at_5", "passed": metrics["recall_at_5"] >= data.get("min_recall", 0.9), "checked": len(recall), "failures": [], "value": metrics["recall_at_5"]})
    return {"results": results, "gates": gates, "metrics": metrics, "score": metrics["recall_at_5"]}


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def _resolve(case: dict, fixtures: dict) -> dict:
    return {**fixtures.get(case.get("fixture", ""), {}), **case}


def _pick(case: dict, view: registry.Policy) -> tuple[list[str], dict[str, str], list[skills.Skill]]:
    connectors = set(case.get("connectors") or [])
    ok, refused = {}, {}
    for sk in skills.library().skills.values():
        allowed, why = registry.allowed(sk, "eval", view)
        if allowed:
            state = rd.readiness(sk, connectors)  # telemetry is unknown here, and unknown does not exclude
            if state["state"] == "unavailable":
                allowed, why = False, "; ".join(state["reasons"])
        if allowed:
            ok[sk.name] = sk
        else:
            refused[sk.name] = why
    chosen = skills.select(case["question"], set(case.get("findings") or []), eligible=lambda s: s.name in ok, requested=case.get("requested"), limit=4)
    return [s.name for s, _, _ in chosen], refused, [s for s, _, _ in chosen]


def _plan(sk: skills.Skill, findings: list[str], namespace: str) -> list[dict]:
    """The steps a skill would run for these findings, with rendered arguments, and without running anything."""
    fake = [{"kind": k, "namespace": namespace, "resources": [f"pod/{namespace}-abc-def"], "owner": f"ReplicaSet/{namespace}-abc"} for k in findings]
    base = {"namespace": namespace, "question": "q", "trace_id": "", "repository": ""}
    out = []
    for step in sk.meta.steps:
        for c in skills.contexts_for(step, fake, base):
            args = skills.render(step.args, c)
            if args is not None:
                out.append({"tool": step.tool, "args": args})
    return out


async def run_skills(data: dict) -> dict[str, Any]:
    fixtures = data.get("fixtures") or {}
    results, failures = [], {"selection": [], "rejection": [], "tools": [], "scope": [], "declared_cases": [], "publication": [], "detection": [], "unsupported_remediation": []}
    checked = dict.fromkeys(failures, 0)
    await registry.refresh(force=True)
    for raw in data["cases"]:
        case = _resolve(raw, fixtures)
        exp = raw.get("expect") or {}
        view = registry.Policy(off=set(raw.get("disabled") or []))
        picked, refused, chosen = _pick(case, view)
        checks = {}
        if exp.get("none_selected"):
            checks["selection"] = not picked
        for name in exp.get("selected") or []:
            checks["selection"] = checks.get("selection", True) and name in picked
        for name in exp.get("rejected") or []:
            checks["rejection"] = checks.get("rejection", True) and name not in picked and name in refused
        if exp.get("tools"):
            plan = [p["tool"] for sk in chosen for p in _plan(sk, case.get("findings") or [], "payments")]
            checks["tools"] = all(t in plan for t in exp["tools"])
        if exp.get("argument_namespace"):
            plan = [p for sk in chosen for p in _plan(sk, case.get("findings") or [], exp["argument_namespace"])]
            checks["scope"] = bool(plan) and all(p["args"].get("namespace", exp["argument_namespace"]) == exp["argument_namespace"] for p in plan)
        for k, v in checks.items():
            checked[k] += 1
            if not v:
                failures[k].append(f"{raw['id']}: chose {picked}")
        results.append({"case": raw["id"], "picked": picked, "checks": checks, "score": round(sum(checks.values()) / len(checks), 4) if checks else 1.0})
    lib = skills.library()
    for sk in lib.skills.values():
        checked["publication"] += 1
        bad = errors(validate(sk, lib.skills))
        if bad:
            failures["publication"].append(f"{sk.name}: {bad[0]}")
        for e in sk.meta.evaluation:
            checked["declared_cases"] += 1
            fx = fixtures.get(e.fixture)
            if fx is None:
                failures["declared_cases"].append(f"{sk.name}: no fixture {e.fixture}")
                continue
            picked, _, _ = _pick({**fx}, registry.Policy())
            wide = [s.name for s, _, _ in skills.select(fx["question"], set(fx.get("findings") or []), limit=8)]
            if e.expect.selected and sk.name not in picked + wide:
                failures["declared_cases"].append(f"{sk.name}: not selected for {e.fixture}")
            planned = [p["tool"] for p in _plan(sk, fx.get("findings") or [], "payments")]
            for t in e.expect.tools:
                if t not in planned:
                    failures["declared_cases"].append(f"{sk.name}: {t} not planned for {e.fixture}")
    # Identity, deduplication and recovery are decided without a cluster.
    checked["detection"] += 4
    env = uuid.uuid4()
    if detection.fingerprint("r", env, "n", "pod", "a") != detection.fingerprint("r", env, "n", "pod", "a"):
        failures["detection"].append("identity is not stable")
    ids = [r.id for sk in lib.skills.values() for r in sk.meta.detection]
    if len(ids) != len(set(ids)):
        failures["detection"].append("duplicate rule identities")

    class _Spec:
        sustained_checks = 2

    class _State:
        consecutive = 0

    if detection.decide(_State, _Spec, active=False, reopen=False, suppressed=False).split(":")[0] != "wait":
        failures["detection"].append("one check opened an incident")
    if detection.decide(_State, _Spec, active=True, reopen=False, suppressed=False) != "update the open incident":
        failures["detection"].append("a repeat did not update the open incident")
    checked["unsupported_remediation"] += 1
    try:
        skills.parse("---\nname: bad-skill\ndescription: A skill proposing an action that is not allowlisted.\nremediation:\n  - {when: crashloop, action: delete_namespace}\n---\nx", "eval", "custom")
        failures["unsupported_remediation"].append("an unknown action was accepted")
    except ValueError:
        pass
    gates = [_gate(k, v, checked[k]) for k, v in failures.items()]
    return {"results": results, "gates": gates, "metrics": {"cases": len(results), "skills": len(lib.skills), "declared_cases": checked["declared_cases"]},
            "score": round(sum(g["passed"] for g in gates) / len(gates), 4)}
