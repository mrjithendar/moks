"""Skills as bounded contracts, and learning from incidents only through review."""
import uuid

import pytest
from sqlalchemy import select, update

from moks import skills
from moks.skills import registry
from moks.db import session_scope
from moks.models import Evidence, ImprovementProposal, Investigation, SkillGeneration
from moks.skills.validate import errors, validate
from tests.conftest import login, make_user
from tests.test_investigation_graph import cluster, finished, start  # noqa: F401
from moks.orchestrator.engine import Investigator


def skill(steps):
    return skills.parse(f"---\nname: demo-contract\ndescription: A skill written for this test only.\nsteps:\n{steps}---\nbody", "t", "custom")


def test_a_stop_condition_must_say_how_to_escalate():
    sk = skill("  - {tool: k8s_events, args: {namespace: '{namespace}'}, stop_if_finding: crashloop}\n")
    assert any("escalate" in e for e in errors(validate(sk)))


def test_unknown_findings_in_conditions_are_refused():
    sk = skill("  - {tool: k8s_events, args: {namespace: '{namespace}'}, requires_finding: no_such_thing}\n")
    assert any("no_such_thing" in e for e in errors(validate(sk)))


def test_step_ids_must_be_unique():
    sk = skill("  - {id: a, tool: k8s_events, args: {namespace: '{namespace}'}}\n  - {id: a, tool: k8s_nodes}\n")
    assert any("unique" in e for e in errors(validate(sk)))


def test_templates_are_checked_all_the_way_down():
    with pytest.raises(ValueError, match="unknown template"):
        skill("  - {tool: k8s_events, args: {namespace: {deep: ['{nope}']}}}\n")


def test_a_controller_from_owner_references_beats_the_name():
    step = skills.StepSpec(tool="k8s_rollout_history", for_each="finding.crashloop")
    f = {"kind": "crashloop", "namespace": "n", "resources": ["pod/x-1"], "owner": "ReplicaSet/a-b-c", "controller": "Deployment/real-name"}
    assert skills.contexts_for(step, [f], {})[0]["deployment"] == "real-name"
    assert "deployment" not in skills.contexts_for(step, [{**f, "controller": ""}], {})[0]  # nothing owns it: no guess
    assert skills.contexts_for(step, [{k: v for k, v in f.items() if k != "controller"}], {})[0]["deployment"] == "a-b"  # the reference could not be read


def test_what_was_asked_beats_a_generic_finding():
    names = [s.name for s, _, _ in skills.select("checkout timeout to its dependency", {"recent_rollout"})]
    assert "dependency-timeouts" in names


async def run_with(monkeypatch, client, text):
    sk = skills.parse(text, "t", "custom")
    lib = skills.library()
    monkeypatch.setitem(lib.skills, sk.name, sk)

    async def stay():
        return lib
    monkeypatch.setattr(registry, "refresh", stay)  # the test's skill is not in a published generation
    iid = await start(client)
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.id == iid).values(requested_skill=sk.name))
    await Investigator(iid, "t").run()
    return (await finished(iid)).report


async def test_a_required_step_that_did_not_run_is_not_success(client, monkeypatch, cluster):  # noqa: F811
    text = "---\nname: demo-contract\ndescription: A skill written for this test only.\nsteps:\n  - {tool: k8s_events, args: {namespace: '{namespace}', limit: 5}}\n  - {tool: k8s_nodes, required: true, when_question: 'nothing matches this'}\n---\nbody"
    run = next(r for r in (await run_with(monkeypatch, client, text))["skill_runs"] if r["skill"] == "demo-contract")
    assert run["outcome"] == "incomplete" and run["gaps"] and run["steps"][1]["status"] == "skipped" and run["steps"][1]["reason"]


async def test_a_precondition_skips_a_step_with_its_reason(client, monkeypatch, cluster):  # noqa: F811
    text = "---\nname: demo-contract\ndescription: A skill written for this test only.\nsteps:\n  - {tool: k8s_events, required: true, requires_finding: evicted, args: {namespace: '{namespace}'}}\n---\nbody"
    run = next(r for r in (await run_with(monkeypatch, client, text))["skill_runs"] if r["skill"] == "demo-contract")
    assert run["steps"][0]["status"] == "not_applicable" and "evicted" in run["steps"][0]["reason"] and run["outcome"] == "complete"


# ---- proposals ---------------------------------------------------------------------------------

async def setup(client):
    iid = await start(client, "why is checkout crashing")
    await Investigator(iid, "t").run()
    async with session_scope() as db:
        ref = (await db.execute(select(Evidence.ref).where(Evidence.investigation_id == iid, Evidence.ok.is_(True)))).scalars().first()
    author = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(author, ["admin"])
    return iid, ref, author, await login(client, author)


def proposal(iid, ref, **over):
    return {"investigation_id": str(iid), "kind": "runbook", "title": "Restart order for checkout", "body": "Restart payments first, then checkout. password=hunter2hunter2", "evidence": [ref], "owner": "team-payments",
            "regression_fixture": {"question": "why is checkout crashing"}, **over}


async def test_a_proposal_is_a_redacted_draft_and_publishes_nothing(client, cluster):  # noqa: F811
    iid, ref, _, h = await setup(client)
    async with session_scope() as db:
        generations = len((await db.execute(select(SkillGeneration))).all())
    r = await client.post("/moks/api/proposals", json=proposal(iid, ref), headers=h)
    assert r.status_code == 201 and r.json()["state"] == "draft" and "hunter2hunter2" not in r.json()["body"] and r.json()["version"] == 1
    async with session_scope() as db:
        assert len((await db.execute(select(SkillGeneration))).all()) == generations


async def test_a_proposal_needs_real_evidence_and_a_fixture(client, cluster):  # noqa: F811
    iid, ref, _, h = await setup(client)
    assert (await client.post("/moks/api/proposals", json=proposal(iid, "E999"), headers=h)).status_code == 422
    assert (await client.post("/moks/api/proposals", json=proposal(iid, ref, regression_fixture={}), headers=h)).status_code == 422
    assert (await client.post("/moks/api/proposals", json=proposal(iid, ref, owner=""), headers=h)).status_code == 422


async def test_an_author_cannot_review_their_own_proposal(client, cluster):  # noqa: F811
    iid, ref, _, h = await setup(client)
    pid = (await client.post("/moks/api/proposals", json=proposal(iid, ref), headers=h)).json()["id"]
    assert (await client.post(f"/moks/api/proposals/{pid}/review", json={"decision": "approved", "note": "looks right"}, headers=h)).status_code == 403
    other = f"b-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(other, ["admin"])
    h2 = await login(client, other)
    r = await client.post(f"/moks/api/proposals/{pid}/review", json={"decision": "approved", "note": "looks right"}, headers=h2)
    assert r.status_code == 200 and r.json()["state"] == "approved"
    assert (await client.post(f"/moks/api/proposals/{pid}/review", json={"decision": "rejected", "note": "changed my mind"}, headers=h2)).status_code == 409


async def test_a_proposal_cannot_be_made_from_what_you_cannot_read(client, cluster):  # noqa: F811
    iid, ref, _, _ = await setup(client)
    outsider = f"c-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(outsider, ["investigator:elsewhere:*"])
    h = await login(client, outsider)
    assert (await client.post("/moks/api/proposals", json=proposal(iid, ref), headers=h)).status_code == 404
    async with session_scope() as db:
        assert not (await db.execute(select(ImprovementProposal).where(ImprovementProposal.investigation_id == iid, ImprovementProposal.created_by != None))).first()  # noqa: E711
