"""Passage-level citations and relevance that weighs explicit relationships before weak ones.

One kb_search stores several passages under one evidence ref. Before passage ids, citing that ref marked every
usable passage as used, including ones the model was never given. These tests pin the four states (retrieved,
eligible, supplied, cited) and the conflict rules in classify.
"""
import json
import re
import uuid

from sqlalchemy import update

from moks.db import session_scope
from moks.models import LLMConfig
from moks.orchestrator import knowledge
from moks.orchestrator.report import Cause, Fact, Report, validate_llm_report
from tests.test_investigation_graph import cluster, plan  # noqa: F401
from tests.test_knowledge_retrieval import RUNBOOK, GitHub, ask, environment, github, no_model, source, sync  # noqa: F401

OOM = """# Payments memory

## OOMKilled after a crash loop

When payments pods crash-loop with OOMKilled, compare the container memory limit with the heap size before raising limits.
"""
DEPLOY = """# Payments deploys

## Crash loop after a deploy

If payments starts crash-looping right after a rollout, roll back with `kubectl -n shop rollout undo deploy/payments`.
"""


class Inv:
    """Just enough of an investigation for the knowledge functions."""

    def __init__(self, passages):
        self.knowledge = {**knowledge.new_state(), "passages": passages}


def p(ref, rank, authority="target", provided=True, path="docs/a.md"):
    return {"id": f"{ref}.{rank}", "ref": ref, "rank": rank, "chunk_id": f"c-{ref}-{rank}", "authority": authority, "basis": [], "excluded": None, "provided": provided,
            "used": False, "cited": False, "path": path, "query": 0, "freshness": {}, "excerpt": ""}


# ---- passage ids and states -----------------------------------------------------------------------


async def test_a_search_with_several_passages_credits_only_the_one_the_answer_cites(client, cluster, github, monkeypatch):  # noqa: F811
    env, email = await environment()
    github["gh"] = GitHub({"acme/payments": {"docs/runbook.md": RUNBOOK, "docs/oom.md": OOM, "docs/deploy.md": DEPLOY}})
    await sync(await source("acme/payments", env, service="payments"))
    from moks.llm import budget
    from moks.llm.providers import LLMResponse
    from moks.orchestrator import engine
    from tests.test_investigation_graph import Model

    seen: dict = {}

    async def complete(provider, system, messages, **kw):
        body = messages[-1]["content"] if isinstance(messages[-1], dict) else str(messages[-1])
        if "tool_calls" in system:
            return LLMResponse(plan(done=True), 10, 5, "fake", "m")
        first = re.findall(r"\[(E\d+\.\d+)\] about the target", body)[0].split(".")[0]
        ids = [i for i in re.findall(r"\[(E\d+\.\d+)\] about the target", body) if i.split(".")[0] == first]  # the passages of one search
        seen["ids"] = ids
        chosen = ids[1]  # the second passage only
        return LLMResponse(json.dumps({"summary": "payments crash-loops.", "observed_facts": [], "suspected_causes": [], "missing_evidence": [],
                                       "next_steps": [f"Documented in the runbook ({chosen}): follow it."], "proposed_remediation": []}), 10, 5, "fake", "m")
    monkeypatch.setattr(budget, "complete", complete)
    monkeypatch.setattr(engine, "provider_for", lambda cfg: Model([]))
    async with session_scope() as db:
        await db.execute(update(LLMConfig).where(LLMConfig.purpose == "chat").values(is_active=False))
        db.add(LLMConfig(name=f"pc-{uuid.uuid4().hex[:6]}", purpose="chat", provider="openai", model="m", is_active=True))
    try:
        inv = await ask(client, env, email)
    finally:
        await no_model()
    k = inv.report["knowledge"]
    assert len(seen["ids"]) >= 3  # several passages from one search, all supplied
    cited = [x for x in k["passages"] if x["cited"]]
    assert [x["id"] for x in cited] == [seen["ids"][1]] and cited[0]["state"] == "cited" and cited[0]["cited_by"] == "model"
    others = [x for x in k["passages"] if x["id"] in seen["ids"] and x["id"] != seen["ids"][1]]
    assert others and all(x["state"] == "supplied" and not x["used"] and x["excluded"] == "not cited in the answer" for x in others)
    assert k["counts"]["cited"] == 1 and k["counts"]["supplied"] >= 3 and k["version"] == 2
    assert all(x["document_id"] for x in k["passages"])  # linked to the document, revision and lines as well as the chunk


def test_a_bare_search_ref_with_several_supplied_passages_credits_none():
    inv = Inv([p("E1", 1), p("E1", 2), p("E1", 3)])
    knowledge.mark_used(inv, Report(generated_by="llm", next_steps=["See the runbook (E1)."]))
    assert not any(x["cited"] for x in inv.knowledge["passages"])
    assert inv.knowledge["citations"]["ambiguous"] == ["E1"]
    assert all("without naming a passage" in x["excluded"] for x in inv.knowledge["passages"])


def test_a_bare_ref_names_a_passage_when_only_one_was_available():
    inv = Inv([p("E1", 1), p("E1", 2, provided=False)])
    knowledge.mark_used(inv, Report(generated_by="llm", next_steps=["See the runbook (E1)."]))
    a, b = inv.knowledge["passages"]
    assert a["cited"] and not b["cited"] and b["state"] == "eligible"
    assert b["excluded"] == "eligible, but not given to the model within the prompt budget"


def test_a_model_cannot_cite_a_passage_it_was_not_given():
    inv = Inv([p("E1", 1), p("E1", 2, provided=False)])
    rep = validate_llm_report({"summary": "s", "observed_facts": [{"statement": "The runbook documents a restart.", "evidence": ["E1.2"]}], "next_steps": ["E1.2 says restart."]},
                              {"E1"}, [], knowledge.supplied(inv.knowledge))
    assert rep.observed_facts[0].evidence == [] and rep.validation["passage_citations_rejected"] == ["E1.2"]
    knowledge.mark_used(inv, rep)
    assert not any(x["cited"] for x in inv.knowledge["passages"])
    assert inv.knowledge["citations"]["rejected"] == [{"id": "E1.2", "reason": "cited, but this passage was not available to the answer"}]


def test_a_claim_citing_a_passage_is_validated_against_its_evidence_and_keeps_the_passage():
    inv = Inv([p("E2", 1), p("E2", 2)])
    rep = validate_llm_report({"summary": "s", "observed_facts": [{"statement": "x", "evidence": ["E2.2", "E5"]}],
                               "suspected_causes": [{"cause": "y", "evidence": ["E2.1"], "contradicting": []}]}, {"E2", "E5"}, [], knowledge.supplied(inv.knowledge))
    assert rep.observed_facts[0].evidence == ["E2", "E5"] and rep.observed_facts[0].passages == ["E2.2"]
    assert rep.suspected_causes[0].passages == ["E2.1"]
    rep.suspected_causes[0].evidence = []  # validation removed the citation, so the passage no longer counts
    knowledge.mark_used(inv, rep)
    assert [x["id"] for x in inv.knowledge["passages"] if x["cited"]] == ["E2.2"]


def test_the_prompt_budget_decides_what_is_supplied(monkeypatch):
    inv = Inv([p("E1", r) for r in range(1, 6)])
    inv.evidence = [{"tool": "kb_search", "ok": True, "data": [{"chunk_id": f"c-E1-{r}", "text": "x" * 3000} for r in range(1, 6)]}]
    monkeypatch.setattr(knowledge, "PROMPT_TOTAL_CHARS", 2600)
    for x in inv.knowledge["passages"]:
        x["provided"] = False
    block = knowledge.prompt_block(inv)
    given = [x["id"] for x in inv.knowledge["passages"] if x["provided"]]
    assert given and len(given) < 5 and all(f"[{i}]" in block for i in given)
    assert not any(f"[{x['id']}]" in block for x in inv.knowledge["passages"] if not x["provided"])


def test_rules_reports_cite_passage_ids_and_credit_rules():
    inv = Inv([p("E1", 1, provided=False), p("E1", 2, provided=False, authority="shared", path="docs/b.md")])
    steps = knowledge.documented_steps(inv, 1)
    assert "(E1.1)" in steps[0]
    knowledge.mark_used(inv, Report(generated_by="rules", next_steps=steps))
    a, b = inv.knowledge["passages"]
    assert a["cited"] and a["cited_by"] == "rules" and not b["cited"]


def test_guidance_items_cite_passages_by_id_and_old_ref_path_pairs_still_resolve():
    inv = Inv([p("E1", 1, path="docs/a.md"), p("E1", 2, path="docs/b.md")])
    knowledge.mark_used(inv, Report(generated_by="llm", answer={"steps": [{"text": "t", "sources": ["E1.2"], "path": "docs/b.md"}]}))
    assert [x["id"] for x in inv.knowledge["passages"] if x["cited"]] == ["E1.2"]
    inv = Inv([p("E1", 1, path="docs/a.md"), p("E1", 2, path="docs/b.md")])
    knowledge.mark_used(inv, Report(generated_by="llm", answer={"steps": [{"text": "t", "sources": ["E1"], "path": "docs/a.md"}]}))  # a guidance answer from before ids
    assert [x["id"] for x in inv.knowledge["passages"] if x["cited"]] == ["E1.1"]


def test_historical_passages_without_ids_or_states_still_render():
    old = {"ref": "E3", "chunk_id": "c", "query": 0, "rank": 2, "authority": "target", "basis": [], "excluded": None, "used": True, "provided": True, "path": "a.md", "freshness": {}}
    inv = type("I", (), {"evidence": [], "knowledge": None})()
    st = knowledge.restore(inv, {"status": "retrieved", "passages": [dict(old)]})
    assert st["passages"][0]["id"] == "E3.2" and st["passages"][0]["state"] == "cited"
    inv.knowledge = st
    out = knowledge.summary(inv)
    assert out["counts"]["cited"] == 1 and out["counts"]["used"] == 1  # version 1 readers keep working


def test_a_demoted_fact_carries_its_passages_into_the_hypothesis():
    from moks.orchestrator import evidence as ev
    from moks.orchestrator.report import validate_claims

    ledger = ev.Ledger.build([{"ref": "E1", "tool": "kb_search", "ok": True, "summary": "runbook", "args": {}, "data": [{"text": "restart payments"}]}], namespace="shop")
    rep = Report(generated_by="llm", observed_facts=[Fact(statement="payments is healthy", evidence=["E1"], passages=["E1.1"])])
    validate_claims(rep, ledger, [])
    assert not rep.observed_facts  # documentation cannot show health
    assert rep.suspected_causes[0].origin == "demoted_fact" and rep.suspected_causes[0].passages == ["E1.1"]
    assert Cause(cause="c").passages == []


# ---- conflicting target relevance -----------------------------------------------------------------


def hit(service=None, namespace="shop", text="crash loop restart previous logs", repo="acme/runbooks", path="docs/runbook.md", applies_to=None):
    return {"citation": {"repository": repo, "path": path, "source_type": "github"}, "scope": {"namespace": namespace, "service": service, "applies_to": applies_to or {}},
            "title": "runbook", "heading": "", "text": text}


CX = {"names": {"payments"}, "repository": None, "namespaces": {"shop"}, "identifiers": set(), "terms": {"crash", "loop", "restart", "logs"}}


def test_a_runbook_for_another_service_in_the_same_namespace_is_not_the_targets():
    authority, basis, excluded = knowledge.classify(hit(service="billing"), CX, 0, "keyword")
    assert authority == "unrelated" and "written for billing" in excluded
    assert any("supporting context only" in b for b in basis)


def test_a_namespace_match_alone_is_supporting_context_not_proof():
    authority, basis, _ = knowledge.classify(hit(), CX, 0, "keyword")
    assert authority == "shared" and any("not specific to the target" in b for b in basis)
    assert not any(b.startswith("scoped to namespace") and "supporting" not in b for b in basis)


def test_a_runbook_for_another_service_that_mentions_the_target_stays_unverified():
    authority, basis, _ = knowledge.classify(hit(service="billing", text="billing crash loop restart logs; payments calls billing"), CX, 0, "keyword")
    assert authority == "unverified" and any("billing" in b for b in basis) and any("mentions payments" in b for b in basis)


def test_the_targets_own_mapping_still_wins_inside_a_shared_namespace():
    authority, basis, _ = knowledge.classify(hit(service="payments"), CX, 0, "keyword")
    assert authority == "target" and basis[0] == "source maps it to service payments"


def test_declared_dependency_and_shared_documentation():
    authority, basis, _ = knowledge.classify(hit(service="postgres", applies_to={"services": ["postgres"], "dependency_of": ["payments", "cart"]}), CX, 0, "keyword")
    assert authority == "dependency" and "dependency of payments" in basis[0]
    authority, basis, _ = knowledge.classify(hit(applies_to={"shared": True}, path="ops/crash.yaml"), CX, 0, "keyword")
    assert authority == "shared" and any("declares it shared" in b for b in basis)


def test_insufficient_evidence_keeps_the_relationship_unverified():
    authority, basis, _ = knowledge.classify(hit(namespace="*", path="scripts/restart.sh"), CX, 0, "keyword")
    assert authority == "unverified" and any("not established" in b for b in basis)


async def test_two_services_in_one_namespace_end_to_end(client, cluster, github):  # noqa: F811
    from tests.test_knowledge_retrieval import BILLING

    await no_model()
    env, email = await environment()
    github["gh"] = GitHub({"acme/billing": {"docs/runbook.md": BILLING + "\nScoped to namespace shop.\n"}})
    await sync(await source("acme/billing", env, namespace="shop", service="billing"))
    inv = await ask(client, env, email, "why is payments in CrashLoopBackOff")
    x = next(x for x in inv.report["knowledge"]["passages"] if x["repository"] == "acme/billing")
    assert x["authority"] == "unrelated" and not x["cited"]
    assert "billing-ctl unlock" not in json.dumps(inv.report["next_steps"])
