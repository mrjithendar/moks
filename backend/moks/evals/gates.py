"""Enterprise evaluation gates. Policy and safety are decided by deterministic assertions, never by a
model judging a model. A gate is passed, failed, skipped or not_applicable; the score only summarizes quality.

rag    real ingestion and ranking in PostgreSQL over synthetic documents: recall@k, MRR, nDCG, exact
       identifiers, citation accuracy, claim support, access isolation, abstention, freshness, conflicts,
       injection through retrieved text, failure fallback, latency and measured cost.
skills the production eligibility and selection, planned tools and arguments, real execution of fixtures through
       the orchestrator, required evidence, verification, version pinning, disabled and unavailable skills,
       publication validation, declared evaluation cases, detection identity and recovery.

A gate with nothing to check is skipped, which fails the run, unless a dataset says why it does not apply. That
reason is reported with it. `evidence` says what kind of proof a gate is: deterministic, simulated or live.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select

from .. import detection, skills
from ..config import get_settings
from ..db import session_scope
from ..models import IndexOutbox, KnowledgeSource, LLMConfig, LLMUsage
from ..orchestrator import prompts
from ..rag import access, service
from ..rag.backends import MAPPING_VERSION
from ..rag.chunking import PIPELINE_VERSION
from ..rag.rerank import Outcome
from ..rag.secrets import clean
from ..rbac import Binding, Principal
from ..skills import registry
from ..skills.selection import eligible
from ..skills.validate import errors, validate
from ..tools.builtin import REGISTRY


class IsolationError(Exception):
    """The boundary around evaluation content could not be shown to hold, so the run stops."""


def _gate(name: str, failures: list[str], total: int, evidence: str = "deterministic", applicable: dict | None = None) -> dict:
    """Nothing checked is not a pass: `skipped` gates fail the run unless a dataset marks them not applicable.
    `evidence` says what kind of proof this is: deterministic, simulated or live."""
    why = (applicable or {}).get(name)
    state = ("not_applicable" if why else "skipped") if not total else "failed" if failures else "passed"
    gate = {"name": name, "state": state, "passed": state == "passed", "checked": total, "failures": failures[:20], "evidence": evidence}
    return gate | ({"reason": why} if state == "not_applicable" else {})


def gates_ok(gates: list[dict]) -> bool:
    return bool(gates) and all(g["state"] in ("passed", "not_applicable") for g in gates)


def summary(gates: list[dict]) -> dict[str, int]:
    return {state: sum(g["state"] == state for g in gates) for state in ("passed", "failed", "skipped", "not_applicable")}


async def _spent_since(start) -> dict:
    """What really happened, from the same meter as everything else. No figure is assumed."""
    async with session_scope() as db:
        tokens_in, tokens_out, cost = (await db.execute(select(func.coalesce(func.sum(LLMUsage.tokens_in), 0), func.coalesce(func.sum(LLMUsage.tokens_out), 0), func.coalesce(func.sum(LLMUsage.cost_usd), 0))
                                                        .where(LLMUsage.purpose.like("eval%"), LLMUsage.created_at >= start))).one()
    return {"tokens_in": int(tokens_in), "tokens_out": int(tokens_out), "cost_usd": float(cost), "cost_basis": "measured from llm_usage"}


def versions(dataset: dict) -> dict:
    lib = skills.library()
    return {"dataset": hashlib.sha256(json.dumps(dataset, sort_keys=True).encode()).hexdigest()[:16], "skills_generation": lib.generation, "skills_digest": lib.digest,
            "pipeline": PIPELINE_VERSION, "prompt": hashlib.sha256((prompts.PLANNER_SYSTEM + prompts.REPORT_SYSTEM).encode()).hexdigest()[:16], "mapping": MAPPING_VERSION,
            "index": "evaluation realm: keyword and identifier ranking, no vectors", "reranker": get_settings().rag_rerank_model or None}


async def _active_model() -> str | None:
    async with session_scope() as db:
        cfg = (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat", LLMConfig.is_active.is_(True)))).scalars().first()
        return f"{cfg.provider}/{cfg.model}" if cfg else None


def _ndcg(ranked: list[str], relevant: set[str]) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, d in enumerate(ranked) if d in relevant)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(relevant), len(ranked)))) or 1
    return dcg / ideal


async def _outbox_size() -> int:
    async with session_scope() as db:
        return (await db.execute(select(func.count()).select_from(IndexOutbox).where(IndexOutbox.status == "pending"))).scalar_one()


async def _prove_isolated(words: str, people: dict, by_doc: dict) -> list[str]:
    """From outside the realm, with the widest grant there is, nothing of this run may be visible."""
    token = access.enter_realm(access.OPERATIONAL)
    try:
        wide = Principal("00000000-0000-0000-0000-00000000ffff", "isolation@eval", [Binding("admin", "*", "*")])
        async with session_scope() as db:
            found = await service.search(db, wide, words, 20)
            ids = {uuid.UUID(h["document_id"]) for h in found["results"]}
        return [str(i) for i in ids & set(by_doc)]
    finally:
        access.leave_realm(token)


async def _remove_run(tag: str) -> None:
    async with session_scope() as db:
        await db.execute(delete(KnowledgeSource).where(KnowledgeSource.name.like(f"eval-{tag}-%"), KnowledgeSource.realm == access.EVALUATION))


async def run_rag(data: dict, run_id: uuid.UUID) -> dict[str, Any]:
    tag = str(run_id)[:8]
    env = lambda e: f"eval-{tag}-{e}"  # noqa: E731
    started = datetime.now(timezone.utc)
    await service.cleanup_evaluations()
    outbox_before = await _outbox_size()
    token = access.enter_realm(access.EVALUATION)
    try:
        return await _run_rag(data, tag, env, started, outbox_before)
    finally:
        access.leave_realm(token)
        await _remove_run(tag)  # also after a crash in the middle; cleanup_evaluations catches what this cannot


async def _run_rag(data: dict, tag: str, env, started, outbox_before: int) -> dict[str, Any]:
    by_doc: dict[uuid.UUID, str] = {}
    async with session_scope() as db:
        sources: dict[tuple, KnowledgeSource] = {}
        for d in data["documents"]:
            key = (d["environment"], d["namespace"])
            if key not in sources:
                sources[key] = KnowledgeSource(name=f"eval-{tag}-{len(sources)}", type="upload", config={}, scope_environment=env(key[0]), scope_namespace=key[1], classification="internal", realm=access.EVALUATION)
                db.add(sources[key])
                await db.flush()
            doc = await service.index_document(db, sources[key], d["path"], d["text"], revision=tag, url=None, extra={"service": d.get("service")})
            by_doc[doc.id] = d["id"]
    people = {name: Principal(f"00000000-0000-0000-0000-{i:012d}", f"{name}@eval", [Binding(r, env(e) if e != "*" else "*", n) for r, e, n in rows])
              for i, (name, rows) in enumerate(data["bindings"].items())}
    leaks = await _prove_isolated(" ".join(c["query"] for c in data["cases"][:5])[:400], people, by_doc)
    if leaks:
        raise IsolationError(f"evaluation documents were visible to an operational search: {leaks[:3]}")
    results, recall, mrr, ndcg, latency = [], [], [], [], []
    failures: dict[str, list[str]] = {"isolation": [], "exact_identifier": [], "abstention": [], "freshness": [], "conflicts": [], "citation_accuracy": [], "claim_support": [], "injection": [], "failure_fallback": [], "realm_isolation": [], "vector_retrieval": [], "injection_with_a_model": []}
    checked = dict.fromkeys(failures, 0)
    before = {n: s.digest for n, s in skills.library().skills.items()}
    known = set(by_doc)
    stored = {d["id"]: clean(d["text"]) for d in data["documents"]}
    for case in data["cases"]:
        start = time.monotonic()
        async with session_scope() as db:
            r = await service.search(db, people[case["as"]], case["query"], 5, environment=env(case["environment"]) if case.get("environment") else None)
        latency.append(time.monotonic() - start)
        ids = [uuid.UUID(h["document_id"]) for h in r["results"]]
        checked["realm_isolation"] += 1
        strangers = [str(i) for i in ids if i not in known]
        if strangers:  # an evaluation must never read a production document
            failures["realm_isolation"].append(f"{case['id']}: {strangers[:3]}")
        ranked = [by_doc[i] for i in ids if i in by_doc]
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
        if case.get("conflict"):
            checked["conflicts"] += 1
            flagged = {by_doc.get(uuid.UUID(h["document_id"])) for h in r["results"] if h["freshness"].get("possible_conflict")}
            ok = set(case["conflict"]) <= flagged and r["evidence"]["quality"]["possible_conflicts"] >= 2
            row["checks"]["conflicts"] = ok
            if not ok:
                failures["conflicts"].append(f"{case['id']}: {sorted(x for x in flagged if x)}")
        for hit in r["results"]:
            checked["citation_accuracy"] += 1
            why = _citation_fault(hit, stored.get(by_doc.get(uuid.UUID(hit["document_id"]), "")))
            if why:
                failures["citation_accuracy"].append(f"{case['id']}: {why}")
        for claim in case.get("claims") or []:
            checked["claim_support"] += 1
            got = service.claim_support(claim["text"], r["results"])["state"]
            ok = got == ("supported" if claim["supported"] else "unsupported")
            row["checks"]["claim_support"] = row["checks"].get("claim_support", True) and ok
            if not ok:
                failures["claim_support"].append(f"{case['id']}: {claim['text'][:60]} was {got}")
        if case.get("gate") == "injection":
            checked["injection"] += 1
            why = _injection_fault(r, relevant, ranked, before, people[case["as"]])
            row["checks"]["injection"] = not why
            if why:
                failures["injection"].append(f"{case['id']}: {why}")
        row["score"] = round(sum(row["checks"].values()) / len(row["checks"]), 4) if row["checks"] else None
        results.append(row)
    await _fallback_gate(data, people, env, failures, checked)
    if await _outbox_size() != outbox_before:
        failures["realm_isolation"].append("the operational outbox changed during the run")
    latency.sort()
    metrics = {"recall_at_5": _mean(recall), "mrr": _mean(mrr), "ndcg_at_5": _mean(ndcg), "latency_p50_ms": round(latency[len(latency) // 2] * 1000, 1), "latency_p95_ms": round(latency[int(len(latency) * 0.95)] * 1000, 1), "latency_max_ms": round(latency[-1] * 1000, 1),
               **await _spent_since(started), "mode": results[0]["mode"] if results else None, "model": await _active_model(), "embedding_model": None}
    simulated = {"injection", "failure_fallback"}
    gates = [_gate(k, v, checked[k], "simulated" if k in simulated else "deterministic", data.get("not_applicable")) for k, v in failures.items()]
    gates.append({"name": "recall_at_5", "state": "passed" if recall and metrics["recall_at_5"] >= data.get("min_recall", 0.9) else "failed" if recall else "skipped", "evidence": "deterministic",
                  "passed": bool(recall) and metrics["recall_at_5"] >= data.get("min_recall", 0.9), "checked": len(recall), "failures": [], "value": metrics["recall_at_5"]})
    metrics["gates"] = summary(gates)
    return {"results": results, "gates": gates, "metrics": metrics, "score": metrics["recall_at_5"]}


def _is_framed(text: str) -> bool:
    """A retrieved passage reaches the model inside a block it cannot close, whatever it says."""
    wrapped = prompts.untrusted("kb", text, 4000)
    return wrapped.count("</untrusted_data") == 1 and wrapped.startswith("<untrusted_data")


def _citation_fault(hit: dict, stored: str | None) -> str | None:
    """A citation must point at exactly the text it came with. The stored document is the reference, not the chunk."""
    c = hit["citation"]
    if stored is None or not c["path"]:
        return f"{hit['chunk_id']}: no document to check it against"
    a, b = c["offsets"]
    if stored[a:b] != hit["text"]:
        return f"{c['path']}: offsets {a}-{b} do not hold the passage"
    if c["lines"] != [stored.count("\n", 0, a) + 1, stored.count("\n", 0, b) + 1]:
        return f"{c['path']}: lines {c['lines']} are not where the passage is"
    return None


def _injection_fault(result: dict, relevant: set[str], ranked: list[str], before: dict, person: Principal) -> str | None:
    """A document that says what to do must not change what can happen. Digests alone cannot show that, so this
    checks each thing such text could try to reach: the instructions, the tool list, the skill set and the grants.
    It proves the wiring. How a real model behaves on seeing this text is not measured here."""
    if not relevant & set(ranked):
        return "the document was not retrieved, so nothing was tested"
    if {n: s.digest for n, s in skills.library().skills.items()} != before:
        return "the skill library changed"
    texts = [h["text"] for h in result["results"]]
    if not all(_is_framed(t) for t in texts):
        return "a passage can close its frame"
    trusted = prompts.PLANNER_SYSTEM + prompts.REPORT_SYSTEM + prompts.skills_section([(s, 0, []) for s in skills.library().skills.values()][:3])
    if any(t[:60] in trusted for t in texts):
        return "retrieved text reached the trusted instructions"
    if any(not spec.read_only for spec in REGISTRY.values() if spec.name in {"kb_search", "git_read_file"}) or any(word in REGISTRY for word in ("approve_action", "execute_action", "publish_skill", "grant_role")):
        return "a tool the text asks for exists"
    if access.grants(person) != access.grants(Principal(person.user_id, person.email, list(person.bindings))):
        return "the grants changed"
    return None


class _FailingReranker:
    """Stands in for a service that times out, through the path a real failure takes."""
    candidates = 30

    async def rerank(self, query: str, items: list) -> Outcome:
        return Outcome(reason="timeout")


async def _fallback_gate(data: dict, people: dict, env, failures: dict, checked: dict) -> None:
    """With the reranker failing, search answers exactly as it does with reranking off, and says so."""
    for case in [c for c in data["cases"] if c.get("relevant")][:5]:
        scope = {"environment": env(case["environment"]) if case.get("environment") else None}
        async with session_scope() as db:
            plain = await service.search(db, people[case["as"]], case["query"], 5, rerank=False, **scope)
            broken = await service.search(db, people[case["as"]], case["query"], 5, reranker=_FailingReranker(), **scope)
        checked["failure_fallback"] += 1
        same = [h["chunk_id"] for h in plain["results"]] == [h["chunk_id"] for h in broken["results"]]
        if not same or (broken.get("reranking") or {}).get("state") != "fallback":
            failures["failure_fallback"].append(f"{case['id']}: {(broken.get('reranking') or {}).get('state')}")


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def _resolve(case: dict, fixtures: dict) -> dict:
    return {**fixtures.get(case.get("fixture", ""), {}), **case}


def _pick(case: dict, view: registry.Policy) -> tuple[list[str], dict[str, str], list[skills.Skill]]:
    """The same eligibility and selection an investigation uses, with the same limit and resources. Telemetry is
    unknown here, and unknown does not exclude, in production either."""
    findings = [{"kind": k, "resources": [f"pod/{k}-0"]} for k in case.get("findings") or []]
    lib = skills.library().skills
    ok, refused = eligible(lib, "eval", view, set(case.get("connectors") or []), resources=skills.resources_of(findings))
    chosen = skills.select(case["question"], set(case.get("findings") or []), eligible=lambda s: s.name in ok, requested=case.get("requested"), resources=skills.resources_of(findings))
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


def _check_execution(case: dict, run: dict | None, pins: dict, report: dict) -> list[str]:
    """What really happened in one fixture run, against what the case expects."""
    exp, bad = case.get("expect") or {}, []
    if exp.get("not_selected"):
        return [] if run is None else [f"{case['skill']} ran"]
    if run is None:
        return [f"{case['skill']} did not run"]
    done = [s["tool"] for s in run["steps"] if s["status"] == "executed"]
    bad += [f"{t} was not executed" for t in exp.get("executed") or [] if t not in done]
    bad += [f"{t} is not recorded as skipped" for t in exp.get("skipped") or [] if not any(s["tool"] == t and s["status"] in ("skipped", "failed", "not_applicable") and (s["reason"] or s["errors"]) for s in run["steps"])]
    if any(s["status"] == "skipped" and not s["reason"] for s in run["steps"]):
        bad.append("a step was skipped without a reason")
    if exp.get("outcome") and run.get("outcome") != exp["outcome"]:
        bad.append(f"outcome was {run.get('outcome')}: {run.get('gaps')}")
    if run.get("digest") != pins.get(case["skill"]) or not run.get("version"):
        bad.append("the run did not pin the version it used")
    if exp.get("withheld") and not {w["kind"] for w in report.get("withheld_findings") or []} >= set(exp["withheld"]):
        bad.append(f"verification was not withheld: {report.get('verified_findings')}")
    if exp.get("withheld") and {v["check"].rsplit(": ", 1)[-1] for v in report.get("verified_findings") or []} & set(exp["withheld"]):
        bad.append("a conclusion was verified with required evidence missing")
    if exp.get("never_authorizes") and any(a.get("evidence_complete") for a in report.get("skill_actions") or []):
        bad.append("an action was offered as fully evidenced")
    return bad


async def run_skills(data: dict, executions: list[dict] | None = None) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    fixtures = data.get("fixtures") or {}
    results, failures = [], {"selection": [], "rejection": [], "tools": [], "scope": [], "execution": [], "required_evidence": [], "version_pinning": [], "declared_cases": [], "publication": [], "detection": [], "unsupported_remediation": []}
    checked = dict.fromkeys(failures, 0)
    by_skill: dict[str, list[dict]] = {}
    await registry.refresh(force=True)
    for done in executions or []:
        case, gate = done["case"], "required_evidence" if (done["case"].get("expect") or {}).get("withheld") else "execution"
        bad = _check_execution(case, done["run"], done["pins"], done["report"])
        checked[gate] += 1
        checked["version_pinning"] += done["run"] is not None
        if done["run"] is not None and done["run"].get("digest") != done["pins"].get(case["skill"]):
            failures["version_pinning"].append(case["id"])
        failures[gate] += [f"{case['id']}: {b}" for b in bad]
        results.append({"case": case["id"], "executed": True, "picked": [case["skill"]], "checks": {"execution": not bad}, "score": 0.0 if bad else 1.0, "failures": bad[:5]})
        by_skill.setdefault(case["skill"], []).append({"case": case["id"], "kind": "execution", "passed": not bad, "failures": bad[:5]})
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
        results.append({"case": raw["id"], "picked": picked, "checks": checks, "score": round(sum(checks.values()) / len(checks), 4) if checks else None, "state": "checked" if checks else "not_applicable"})
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
            picked, _, _ = _pick({**fx}, registry.Policy())  # exactly what production selects, with its limit
            before = len(failures["declared_cases"])
            if e.expect.selected and sk.name not in picked:
                failures["declared_cases"].append(f"{sk.name}: not selected for {e.fixture}")
            planned = [p["tool"] for p in _plan(sk, fx.get("findings") or [], "payments")]
            for t in e.expect.tools:
                if t not in planned:
                    failures["declared_cases"].append(f"{sk.name}: {t} not planned for {e.fixture}")
            by_skill.setdefault(sk.name, []).append({"case": e.fixture, "kind": "declared", "passed": len(failures["declared_cases"]) == before, "failures": failures["declared_cases"][before:][:5]})
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
    gates = [_gate(k, v, checked[k], "simulated" if k in ("execution", "required_evidence", "version_pinning") else "deterministic", data.get("not_applicable")) for k, v in failures.items()]
    return {"results": results, "gates": gates, "metrics": {"cases": len(results), "skills": len(lib.skills), "declared_cases": checked["declared_cases"], "executed_cases": len(executions or []), "gates": summary(gates), "by_skill": by_skill, **await _spent_since(started)},
            "score": round(sum(g["passed"] for g in gates) / len(gates), 4)}
