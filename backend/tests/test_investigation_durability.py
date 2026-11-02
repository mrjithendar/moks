"""A worker that lost its claim stops and writes nothing; a crashed run resumes without repeating itself."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text, update

from moks.db import session_scope
from moks.models import ConversationMessage, Evidence, Investigation, InvestigationCheckpoint, InvestigationEvent, RoleBinding
from moks.orchestrator import engine, graph, worker
from moks.orchestrator.engine import Investigator, OwnershipLost
from moks.orchestrator.evidence import HISTORICAL
from tests.test_investigation_graph import REPORT, cluster, finished, llm, plan, start  # noqa: F401


@pytest.fixture(autouse=True)
async def only_this_tests_runs():
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.status.in_(("queued", "running"))).values(status="cancelled", execution_token=None))


async def claimed(client):
    iid = await start(client)
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.id != iid, Investigation.status == "queued").values(status="cancelled"))
    won = await worker.claim("w1")
    assert won.id == iid and won.attempt == 1
    return won


def run_of(won, **kw):
    return Investigator(won.id, "w1", token=won.token, attempt=won.attempt, **kw)


async def lapse(iid):
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.id == iid).values(lease_expires_at=func.now() - timedelta(seconds=1)))


async def replies(iid):
    async with session_scope() as db:
        return (await db.execute(select(func.count()).select_from(ConversationMessage).where(ConversationMessage.investigation_id == iid, ConversationMessage.role == "assistant"))).scalar_one()


async def checkpoint_at(iid, node, state=None):
    """A checkpoint as a run writes it, without running up to it."""
    inv = Investigator(iid, "w0")
    inv.versions = {"llm": None, "prompts": "p"}
    await inv._checkpoint(node, {"logs": "skills", "skills": "rules", "rules": "correlate", "report": "__end__"}.get(node, "logs"), {"namespace": "shop", **(state or {})})


async def test_every_claim_gets_its_own_token(client):
    a = await claimed(client)
    b_id = await start(client)
    b = await worker.claim("w2")
    assert b.id == b_id and a.token != b.token


async def test_a_paused_worker_cannot_renew_a_lapsed_lease(client):
    won = await claimed(client)
    await lapse(won.id)
    run = run_of(won)
    assert await run._renew() is False and run.lost


async def test_the_reaper_fences_the_old_worker_off_everything(client):
    won = await claimed(client)
    run = run_of(won)
    await lapse(won.id)
    await worker.reap_stale()
    assert (await finished(won.id)).status == "failed"
    with pytest.raises(OwnershipLost):
        await run._emit("note", {"message": "late"})
    with pytest.raises(OwnershipLost):
        await run._checkpoint("triage", "logs", {"namespace": "shop"})
    assert await run._finish("completed", {"summary": "stale worker's report"}, None) is False
    inv = await finished(won.id)
    assert inv.status == "failed" and inv.report is None and await replies(won.id) == 1


async def test_a_late_completion_cannot_overwrite_the_reapers_decision(client, cluster):  # noqa: F811
    won = await claimed(client)
    run = run_of(won)

    async def paused():
        await lapse(won.id)
        await worker.reap_stale()
        raise OwnershipLost("gone")
    run._investigate = paused
    await run.run()
    assert (await finished(won.id)).status == "failed" and await replies(won.id) == 1


async def test_a_late_tool_result_is_discarded(client, cluster):  # noqa: F811
    won = await claimed(client)
    run = run_of(won)

    class Slow:
        def __init__(self, db):
            self.db = db

        async def run(self, tool, args):
            await lapse(won.id)
            await worker.reap_stale()
            self.db.add(Evidence(investigation_id=won.id, ref="E1", tool=tool, connector_type="kubernetes", args=args, scope={}, ok=True, summary="late"))
            return engine.ToolOutcome(True, tool, args, "late", evidence_ref="E1")

    async with session_scope() as db:
        with pytest.raises(OwnershipLost):
            await run._call(Slow(db), "k8s_events", {"namespace": "shop"})
    async with session_scope() as db:
        assert (await db.execute(select(func.count()).select_from(Evidence).where(Evidence.investigation_id == won.id))).scalar_one() == 0


async def test_reapers_racing_decide_once(client):
    won = await claimed(client)
    await lapse(won.id)
    await asyncio.gather(*[worker.reap_stale() for _ in range(5)])
    assert await replies(won.id) == 1
    async with session_scope() as db:
        done = (await db.execute(select(func.count()).select_from(InvestigationEvent).where(InvestigationEvent.investigation_id == won.id, InvestigationEvent.type == "done"))).scalar_one()
    assert done == 1


async def test_a_running_worker_keeps_its_claim(client):
    won = await claimed(client)
    run = run_of(won)
    assert await run._renew() is True
    await worker.reap_stale()
    assert (await finished(won.id)).status == "running"


async def test_a_database_outage_past_the_lease_stops_the_work(client, monkeypatch):
    won = await claimed(client)
    run = run_of(won)
    run.settings = run.settings.model_copy(update={"investigation_lease_seconds": 0})

    async def down():
        raise ConnectionError("database gone")
    monkeypatch.setattr(run, "_renew", down)
    monkeypatch.setattr(engine, "WATCH_SECONDS", 0.01)
    task = asyncio.create_task(asyncio.sleep(30))
    await asyncio.wait_for(run._watch(task), 3)
    await asyncio.sleep(0)
    assert run.lost and task.cancelled()


async def test_an_outage_inside_the_lease_is_ridden_out(client, monkeypatch):
    won = await claimed(client)
    run = run_of(won)
    monkeypatch.setattr(engine, "WATCH_SECONDS", 0.01)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] > 3:
            return False
        raise ConnectionError("blip")
    monkeypatch.setattr(run, "_renew", flaky)
    await asyncio.wait_for(run._watch(asyncio.create_task(asyncio.sleep(1))), 3)
    assert not run.lost and calls["n"] == 4


async def test_an_outage_at_the_very_end_leaves_the_decision_to_the_reaper(client, monkeypatch, cluster):  # noqa: F811
    won = await claimed(client)
    run = run_of(won)

    async def nothing():
        raise RuntimeError("bug")

    async def down(*a):
        raise ConnectionError("database gone")
    run._investigate = nothing
    monkeypatch.setattr(run, "_finish", down)
    await run.run()  # must not raise out of the worker's task
    assert (await finished(won.id)).status == "running"


async def test_cancelling_while_the_worker_is_gone_ends_as_cancelled(client):
    won = await claimed(client)
    await checkpoint_at(won.id, "triage")
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.id == won.id).values(cancel_requested=True))
    await lapse(won.id)
    await worker.reap_stale()
    assert (await finished(won.id)).status == "cancelled"


async def test_a_checkpoint_sends_the_run_back_to_the_queue(client):
    won = await claimed(client)
    await checkpoint_at(won.id, "triage")
    await lapse(won.id)
    await worker.reap_stale()
    inv = await finished(won.id)
    assert inv.status == "queued" and inv.execution_token is None and inv.finished_at is None and await replies(won.id) == 0


async def test_attempts_run_out(client):
    won = await claimed(client)
    await checkpoint_at(won.id, "triage")
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.id == won.id).values(attempts=2))
    await lapse(won.id)
    await worker.reap_stale()
    assert (await finished(won.id)).status == "failed"


class Killed(BaseException):
    pass


async def crash_after(monkeypatch, node):
    """Kills the first attempt right after `node` wrote its checkpoint."""
    real = Investigator._checkpoint

    async def dying(self, name, next_node, state):
        await real(self, name, next_node, state)
        if name == node and self.attempt == 1:
            raise Killed  # outruns every handler, as a killed process would
    monkeypatch.setattr(Investigator, "_checkpoint", dying)


async def attempt(won):
    run = run_of(won)
    try:
        await run.run()
    except Killed:
        pass
    return run


async def resume(iid):
    await lapse(iid)
    await worker.reap_stale()
    again = await worker.claim("w2")
    assert again.id == iid and again.attempt == 2
    run = Investigator(again.id, "w2", token=again.token, attempt=again.attempt)
    await run.run()
    return run


async def test_a_killed_run_resumes_and_finishes_once(client, cluster, llm, monkeypatch):  # noqa: F811
    llm["replies"] = [plan(("k8s_events", {"namespace": "shop"})), plan(done=True), REPORT]
    won = await claimed(client)
    await crash_after(monkeypatch, "act")
    await attempt(won)
    calls_before, tokens = llm["calls"], (await session_count(won.id))
    run = await resume(won.id)
    inv = await finished(won.id)
    assert inv.status == "completed" and inv.report["execution"] == {**inv.report["execution"], "attempt": 2, "resumed_after": "act"}
    assert await replies(won.id) == 1
    assert llm["calls"] == calls_before + 2  # the plan step and the report, not the rounds already paid for
    assert tokens > 0 and "triage" in run.completed_nodes
    async with session_scope() as db:
        assert await db.get(InvestigationCheckpoint, won.id) is None
        refs = (await db.execute(select(Evidence.ref).where(Evidence.investigation_id == won.id))).scalars().all()
    assert len(refs) == len(set(refs))


async def session_count(iid):
    async with session_scope() as db:
        return (await db.execute(select(func.count()).select_from(Evidence).where(Evidence.investigation_id == iid))).scalar_one()


async def test_completed_nodes_do_not_run_again(client, cluster, llm, monkeypatch):  # noqa: F811
    won = await claimed(client)
    await crash_after(monkeypatch, "logs")
    await attempt(won)
    before = await session_count(won.id)
    seen = []
    real = Investigator._baseline

    async def counted(self, *a, **kw):
        seen.append(1)
        return await real(self, *a, **kw)
    monkeypatch.setattr(Investigator, "_baseline", counted)
    run = await resume(won.id)
    assert seen == [] and before > 0 and run.tool_calls >= before


async def test_tool_budget_survives_the_crash(client, cluster, monkeypatch):  # noqa: F811
    won = await claimed(client)
    await crash_after(monkeypatch, "triage")
    await attempt(won)
    used = await session_count(won.id)
    run = await resume(won.id)
    assert run.tool_calls >= used > 0


async def test_aged_evidence_turns_historical_and_is_read_again(client, cluster, monkeypatch):  # noqa: F811
    won = await claimed(client)
    await crash_after(monkeypatch, "logs")
    await attempt(won)
    async with session_scope() as db:
        await db.execute(update(Evidence).where(Evidence.investigation_id == won.id).values(collected_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    await resume(won.id)
    async with session_scope() as db:
        rows = (await db.execute(select(Evidence).where(Evidence.investigation_id == won.id))).scalars().all()
    kinds = {e.kind for e in rows}
    assert HISTORICAL in kinds and "observation" in kinds  # k8s_triage was collected again
    report = (await finished(won.id)).report
    assert report["validation"]["evidence_kinds"]


async def test_resuming_checks_permissions_again(client, cluster, monkeypatch):  # noqa: F811
    won = await claimed(client)
    await crash_after(monkeypatch, "logs")
    await attempt(won)
    owner = (await finished(won.id)).created_by
    async with session_scope() as db:
        await db.execute(RoleBinding.__table__.delete().where(RoleBinding.user_id == owner))
    await resume(won.id)
    inv = await finished(won.id)
    assert inv.status == "failed" and "not permitted" in inv.error


async def test_checkpoints_hold_no_raw_payloads_or_secrets(client, cluster, monkeypatch):  # noqa: F811
    won = await claimed(client)
    await crash_after(monkeypatch, "logs")
    run = run_of(won)
    run.notes.append("password=hunter2hunter2")
    try:
        await run.run()
    except Killed:
        pass
    async with session_scope() as db:
        cp = await db.get(InvestigationCheckpoint, won.id)
        stored = str(cp.state)
    assert "hunter2hunter2" not in stored and "'data'" not in stored and cp.next_node == "skills"
    assert {"completed", "findings", "skills", "budget", "versions", "authorization", "connectors", "scope", "calls_seen", "graph"} <= set(cp.state)
