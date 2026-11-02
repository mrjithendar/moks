"""Does reranking earn its place? The same questions and the same authorized candidates, with and without it.

Runs in the evaluation realm against a real reranking service, so real model output is measured, and no
operational document is read. Nothing here turns reranking on: a pass makes it eligible, and an operator decides.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

from ..config import get_settings
from ..db import session_scope
from ..models import KnowledgeSource
from ..rag import access, rerank, service
from ..rbac import Binding, Principal
from . import gates


def _client() -> rerank.Reranker | None:
    s = get_settings()
    if not (s.rag_rerank_url and s.rag_rerank_model and s.rag_rerank_revision):
        return None
    return rerank.Reranker(s.rag_rerank_url, os.environ.get("MOKS_CRED_RERANKER_TOKEN", ""), s.rag_rerank_model, s.rag_rerank_revision, s.rag_rerank_candidates,
                           rerank.MAX_TIMEOUT_MS, s.rag_rerank_max_query_chars, s.rag_rerank_max_passage_chars)


def _percentile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1) if xs else 0.0


def _measures(ranked: list[str], relevant: set[str]) -> dict[str, float]:
    return {"recall_at_3": len(relevant & set(ranked[:3])) / len(relevant), "recall_at_5": len(relevant & set(ranked[:5])) / len(relevant),
            "mrr": next((1 / (i + 1) for i, d in enumerate(ranked) if d in relevant), 0.0), "ndcg_at_5": gates._ndcg(ranked[:5], relevant)}


def _citations(result: dict) -> tuple:
    return (result["citation"], result["scope"], result["text"], result["title"])


async def run(data: dict, run_id: uuid.UUID) -> dict[str, Any]:
    client = _client()
    if client is None:
        return {"results": [], "gates": [gates._gate("reranking_service", [], 0)], "metrics": {"note": "no reranking service is configured, so nothing was measured"}, "score": 0}
    tag = str(run_id)[:8]
    await service.cleanup_evaluations()
    token = access.enter_realm(access.EVALUATION)
    try:
        return await _compare(data, client, tag)
    finally:
        access.leave_realm(token)
        await gates._remove_run(tag)


async def _compare(data: dict, client: rerank.Reranker, tag: str) -> dict[str, Any]:
    env = lambda e: f"eval-{tag}-{e}"  # noqa: E731
    by_doc: dict[uuid.UUID, str] = {}
    async with session_scope() as db:
        sources: dict[tuple, KnowledgeSource] = {}
        for d in data["documents"]:
            key = (d.get("environment", "prod"), d.get("namespace", "platform"))
            if key not in sources:
                sources[key] = KnowledgeSource(name=f"eval-{tag}-{len(sources)}", type="upload", config={}, scope_environment=env(key[0]), scope_namespace=key[1], classification="internal", realm=access.EVALUATION)
                db.add(sources[key])
                await db.flush()
            doc = await service.index_document(db, sources[key], d["path"], d["text"], revision=tag, url=None, extra={"service": d.get("service")})
            by_doc[doc.id] = d["id"]
    people = {name: Principal(str(uuid.uuid4()), f"{name}@eval", [Binding(r, env(e), n) for r, e, n in rows]) for name, rows in data["bindings"].items()}
    leaks = await gates._prove_isolated(" ".join(c["query"] for c in data["cases"][:5])[:400], people, by_doc)
    if leaks:
        raise gates.IsolationError(f"evaluation documents were visible to an operational search: {leaks[:3]}")
    await service.drain_outbox(0)
    rows: list[dict] = []
    totals: dict[str, dict[str, list[float]]] = {"off": {}, "on": {}}
    failures: dict[str, list[str]] = {"authorization": [], "citations": [], "exact_identifier": [], "fallback": [], "no_answer": []}
    checked = dict.fromkeys(failures, 0)
    latency: dict[str, list[float]] = {"off": [], "on": []}
    inference: list[float] = []
    diversity: dict[str, list[float]] = {"off": [], "on": []}
    truncated = 0
    for case in data["cases"]:
        results: dict[str, dict] = {}
        for mode, reranker in (("off", None), ("on", client)):
            start = time.monotonic()
            async with session_scope() as db:
                results[mode] = await service.search(db, people[case.get("as", "reader")], case["query"], 5, reranker=reranker)
            latency[mode].append((time.monotonic() - start) * 1000)
        ranked = {m: [by_doc[uuid.UUID(h["document_id"])] for h in r["results"]] for m, r in results.items()}
        info = results["on"].get("reranking") or {}
        relevant = set(case.get("relevant") or [])
        row: dict[str, Any] = {"case": case["id"], "category": case.get("category"), "off": ranked["off"][:5], "on": ranked["on"][:5], "reranking": info.get("state"), "reason": info.get("reason")}
        checked["fallback"] += 1
        if results["on"]["results"] and info.get("state") != "applied":
            failures["fallback"].append(f"{case['id']}: {info.get('reason')}")
        if info.get("inference_ms") is not None:
            inference.append(info["inference_ms"])
        truncated += info.get("truncated_passages") or 0
        if relevant:
            for mode in ("off", "on"):
                measured = _measures(ranked[mode], relevant)
                row[f"{mode}_measures"] = {k: round(v, 3) for k, v in measured.items()}
                for k, v in measured.items():
                    totals[mode].setdefault(k, []).append(v)
                diversity[mode].append(len(set(ranked[mode][:5])) / max(len(ranked[mode][:5]), 1))
        if case.get("forbidden"):
            checked["authorization"] += 1
            leak = set(case["forbidden"]) & set(ranked["on"])
            if leak:
                failures["authorization"].append(f"{case['id']}: {sorted(leak)}")
        if case.get("exact"):
            checked["exact_identifier"] += 1
            if ranked["on"][:1] != case["relevant"]:
                failures["exact_identifier"].append(f"{case['id']}: {ranked['on'][:3]}")
        if case.get("no_answer"):
            checked["no_answer"] += 1
            if results["on"]["evidence"]["state"] != results["off"]["evidence"]["state"]:
                failures["no_answer"].append(f"{case['id']}: reranking changed the abstention decision")
        before = {h["chunk_id"]: _citations(h) for h in results["off"]["results"]}
        for h in results["on"]["results"]:
            checked["citations"] += 1
            if h["chunk_id"] in before and before[h["chunk_id"]] != _citations(h):
                failures["citations"].append(f"{case['id']}: {h['chunk_id']}")
        rows.append(row)
    mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else 0.0  # noqa: E731
    summary = {mode: {k: mean(v) for k, v in totals[mode].items()} for mode in totals}
    categories: dict[str, dict] = {}
    for r in rows:
        if "on_measures" in r:
            c = categories.setdefault(r["category"], {"cases": 0, "off_ndcg": [], "on_ndcg": []})
            c["cases"] += 1
            c["off_ndcg"].append(r["off_measures"]["ndcg_at_5"])
            c["on_ndcg"].append(r["on_measures"]["ndcg_at_5"])
    metrics = {"off": summary["off"], "on": summary["on"], "delta": {k: round(summary["on"].get(k, 0) - summary["off"].get(k, 0), 4) for k in summary["off"]},
               "by_category": {k: {"cases": v["cases"], "off_ndcg_at_5": mean(v["off_ndcg"]), "on_ndcg_at_5": mean(v["on_ndcg"])} for k, v in categories.items()},
               "diversity": {m: mean(v) for m, v in diversity.items()}, "latency_ms": {m: {"p50": _percentile(v, 0.5), "p95": _percentile(v, 0.95)} for m, v in latency.items()},
               "inference_ms": {"p50": _percentile(inference, 0.5), "p95": _percentile(inference, 0.95)}, "truncated_passages": truncated, "model": client.model, "revision": client.revision,
               "cases": len(rows), "cost_usd": 0.0, "cost_basis": "local inference; no paid model is called"}
    roll = data.get("rollout") or {}
    added_p95 = metrics["latency_ms"]["on"]["p95"] - metrics["latency_ms"]["off"]["p95"]
    quality = [("ndcg_gain", metrics["delta"].get("ndcg_at_5", 0) >= roll.get("min_ndcg_gain", 0.02), metrics["delta"].get("ndcg_at_5")),
               ("mrr_gain", metrics["delta"].get("mrr", 0) >= roll.get("min_mrr_gain", 0.02), metrics["delta"].get("mrr")),
               ("no_recall_loss", metrics["delta"].get("recall_at_5", 0) >= -roll.get("max_recall_loss", 0.0), metrics["delta"].get("recall_at_5")),
               ("diversity_kept", metrics["diversity"]["on"] >= metrics["diversity"]["off"] - roll.get("max_diversity_loss", 0.02), metrics["diversity"]["on"]),
               ("added_latency", added_p95 <= roll.get("max_added_p95_ms", 300), round(added_p95, 1))]
    out_gates = [gates._gate(k, v, checked[k], "live") for k, v in failures.items()]
    out_gates += [{"name": n, "state": "passed" if ok else "failed", "passed": bool(ok), "checked": len(rows), "failures": [], "value": value, "evidence": "live"} for n, ok, value in quality]
    metrics["recommendation"] = "eligible to enable" if gates.gates_ok(out_gates) else "keep disabled"
    return {"results": rows, "gates": out_gates, "metrics": metrics, "score": summary["on"].get("ndcg_at_5", 0)}
