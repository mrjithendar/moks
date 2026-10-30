import pytest
from sqlalchemy import text

from moks import audit
from moks.db import session_scope
from moks.diagnosis.detectors import detect_pods, detect_services
from moks.connectors.kubernetes import summarize_pod
from moks.orchestrator.report import parse_json_object, validate_llm_report
from moks.redaction import redact, redact_obj
from tests.fakes import ago, pod


# ---- audit ---------------------------------------------------------------------------------

async def test_audit_chain_is_append_only_and_verifiable():
    for i in range(3):
        await audit.record(None, action="test.event", details={"i": i})
    async with session_scope() as db:
        assert (await audit.verify_chain(db))["ok"]
    for stmt in ("UPDATE audit_events SET action = 'x'", "DELETE FROM audit_events", "TRUNCATE audit_events"):
        with pytest.raises(Exception, match="append-only"):
            async with session_scope() as db:
                await db.execute(text(stmt))


async def test_audit_tampering_is_detected():
    await audit.record(None, action="test.tamper", details={"amount": 1})
    async with session_scope() as db:
        # A superuser can bypass the trigger; the hash chain still reveals the edit.
        await db.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update"))
        await db.execute(text("UPDATE audit_events SET details = '{\"amount\": 1000}' WHERE action = 'test.tamper'"))
        await db.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update"))
    async with session_scope() as db:
        result = await audit.verify_chain(db)
    assert result["ok"] is False


async def test_audit_details_are_redacted():
    await audit.record(None, action="test.redact", details={"password": "p@ss", "note": "Authorization: Bearer abcdefghijklmnop"})
    async with session_scope() as db:
        row = (await db.execute(text("SELECT details FROM audit_events WHERE action = 'test.redact'"))).scalar_one()
    assert row["password"] == "[REDACTED]" and "abcdefghijklmnop" not in row["note"]


# ---- redaction -----------------------------------------------------------------------------

@pytest.mark.parametrize("raw,secret", [
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "dozjgNry"),
    ("aws key AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
    ("postgres://app:SuperSecret1@db:5432/x", "SuperSecret1"),
    ('{"password": "hunter2", "user": "bob"}', "hunter2"),
    ("api_key=sk-abcdefghijklmnopqrstuvwxyz", "sk-abcdefghijklmnop"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
    ("token: ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdef"),
])
def test_redaction(raw, secret):
    assert secret not in redact(raw)


def test_redaction_keeps_useful_text():
    line = 'level=error msg="connection refused" host=payments port=8080'
    assert redact(line) == line
    assert redact_obj({"secretKeyRef": {"name": "db", "key": "password"}})["secretKeyRef"]["name"] == "db"


# ---- detectors -----------------------------------------------------------------------------

def test_pod_detectors():
    pods = [summarize_pod(p) for p in (
        pod("a", "ns", {}, waiting="CrashLoopBackOff", last="Error", restarts=5),
        pod("b", "ns", {}, last="OOMKilled", restarts=2),
        pod("c", "ns", {}, waiting="ImagePullBackOff"),
        pod("d", "ns", {}, waiting="CreateContainerConfigError"),
        pod("e", "ns", {}, ready=False),
    )]
    kinds = {f["kind"]: f for f in detect_pods(pods)}
    assert set(kinds) == {"crashloop", "oomkilled", "image_pull", "config_error", "not_ready"}
    assert all(f["suspected_cause"] is None or "confirmed" not in f["suspected_cause"] for f in kinds.values())


def test_service_selector_mismatch_names_the_near_miss():
    svc = {"name": "checkout", "selector": {"app": "checkout-v2"}, "endpoints": {"ready": 0, "notReady": 0}, "matchingPodCount": 0,
           "nearMissPods": [{"pod": "checkout-abc", "labels": {"app": "checkout"}}], "ports": [], "podContainerPorts": []}
    f = detect_services([svc])
    assert f[0]["kind"] == "service_selector_mismatch" and "checkout-abc" in f[0]["detail"] and "502" in f[0]["detail"]


# ---- report parsing ------------------------------------------------------------------------

def test_parse_json_object_tolerates_fences():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        parse_json_object("no json")


def test_report_rejects_schema_violations():
    with pytest.raises(ValueError):
        validate_llm_report({"suspected_causes": [{"cause": "x", "confidence": "certain"}]}, {"E1"}, [])


def test_restarts_and_ooms_are_judged_against_the_window():
    from datetime import datetime, timedelta, timezone

    from moks.diagnosis.detectors import is_unhealthy

    since = datetime.now(timezone.utc) - timedelta(minutes=30)
    old = summarize_pod(pod("old", "ns", {}, last="Completed", restarts=1, last_minutes_ago=3 * 24 * 60))
    recent = summarize_pod(pod("recent", "ns", {}, last="Error", restarts=1, last_minutes_ago=10))
    old_oom = summarize_pod(pod("old-oom", "ns", {}, last="OOMKilled", restarts=5, last_minutes_ago=2 * 24 * 60))
    assert not is_unhealthy(old, since) and is_unhealthy(recent, since) and not is_unhealthy(old_oom, since)
    kinds = {f["kind"]: f for f in detect_pods([old, recent, old_oom], since)}
    assert set(kinds) == {"recent_restarts", "oomkilled_earlier"}
    assert "recent" in kinds["recent_restarts"]["detail"] and "old (" not in kinds["recent_restarts"]["detail"]
    assert kinds["oomkilled_earlier"]["severity"] == "low"
