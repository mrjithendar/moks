"""The skill registry: versions, generations, one policy everywhere, readiness, validation, recorded runs."""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text

from moks import detection, skills
from moks.db import session_scope
from moks.models import AuditEvent, ComponentVersion, DetectionRun, Environment, Investigation, SkillGeneration, SkillPolicy, SkillVersion
from moks.orchestrator.engine import Investigator
from moks.rag import service
from moks.models import KnowledgeSource
from moks.skills import readiness as rd
from moks.skills import registry
from moks.skills.validate import errors, validate
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes
from moks.connectors import registry as conn_registry


def text_of(name="demo-skill", version="1", rule=None, extra="", overrides=""):
    detect = f"detection:\n  - {{id: {rule}, kind: finding, finding: crashloop}}\n" if rule else ""
    return f"---\nname: {name}\nversion: '{version}'\ndescription: A skill written for this test to prove a point.\n{overrides}triggers:\n  keywords: [zebra]\n{detect}{extra}---\nBody.\n"


def write(d: Path, name: str, content: str) -> None:
    (d / name).mkdir(exist_ok=True)
    (d / name / "SKILL.md").write_text(content)


@pytest.fixture(autouse=True)
async def clean():
    async def reset():
        async with session_scope() as db:
            await db.execute(text("DELETE FROM skill_generations"))
            await db.execute(text("DELETE FROM skill_versions"))
            await db.execute(text("DELETE FROM skill_policies"))
        skills.replace_library(skills.load(""))
    await reset()
    await registry.scan(extra_dir="")
    yield
    await reset()


async def active():
    async with session_scope() as db:
        return await registry._active(db)


async def test_builtins_publish_as_one_generation_and_every_version_is_pinned_by_digest():
    gen = await active()
    assert gen.status == "active" and len(gen.manifest) == 51
    lib = skills.library()
    assert lib.generation == gen.generation and lib.digest == gen.digest
    sk = lib.skills["crashloop-triage"]
    assert gen.manifest[sk.name] == sk.digest and sk.path.startswith("registry:")


async def test_a_second_scan_changes_nothing():
    before = await active()
    r = await registry.scan(extra_dir="")
    assert not r["published"] and (await active()).generation == before.generation


async def test_a_custom_skill_waits_until_it_is_published(tmp_path):
    write(tmp_path, "demo-skill", text_of())
    r = await registry.scan(extra_dir=str(tmp_path))
    assert "demo-skill" in r["waiting"] and "demo-skill" not in skills.library().skills
    sk = skills.load(str(tmp_path)).skills["demo-skill"]
    uid = await make_user(f"p-{uuid.uuid4().hex[:6]}@example.com", ["admin"])
    async with session_scope() as db:
        await registry.publish(db, sk.digest, uid, "reviewed in PR 12")
    await registry.refresh(force=True)
    assert "demo-skill" in skills.library().skills
    async with session_scope() as db:
        row = await db.get(SkillVersion, sk.digest)
        audit = (await db.execute(select(AuditEvent).where(AuditEvent.action == "skills.publish", AuditEvent.resource_id == "demo-skill"))).scalars().first()
    assert row.state == "published" and row.published_by == uid and row.reviewed_by == uid and audit.details["digest"] == sk.digest


async def test_versions_are_immutable(tmp_path):
    write(tmp_path, "demo-skill", text_of())
    await registry.scan(extra_dir=str(tmp_path))
    write(tmp_path, "demo-skill", text_of() + "changed\n")
    r = await registry.scan(extra_dir=str(tmp_path))
    assert any("bump it" in e["error"] for e in r["rejected"])


async def test_rollback_restores_a_published_version_in_a_new_generation(tmp_path):
    uid = await make_user(f"r-{uuid.uuid4().hex[:6]}@example.com", ["admin"])
    write(tmp_path, "demo-skill", text_of(version="1"))
    await registry.scan(extra_dir=str(tmp_path))
    one = skills.load(str(tmp_path)).skills["demo-skill"].digest
    async with session_scope() as db:
        await registry.publish(db, one, uid)
    write(tmp_path, "demo-skill", text_of(version="2"))
    await registry.scan(extra_dir=str(tmp_path))
    two = skills.load(str(tmp_path)).skills["demo-skill"].digest
    async with session_scope() as db:
        await registry.publish(db, two, uid)
    await registry.refresh(force=True)
    assert skills.library().skills["demo-skill"].meta.version == "2"
    async with session_scope() as db:
        await registry.rollback(db, "demo-skill", one, uid, "version 2 misfires")
    await registry.refresh(force=True)
    assert skills.library().skills["demo-skill"].meta.version == "1"
    async with session_scope() as db:
        assert (await db.get(SkillVersion, two)).state == "deprecated"
        with pytest.raises(registry.PublishError):
            await registry.rollback(db, "crashloop-triage", one, uid)


async def test_an_invalid_version_cannot_be_published_and_the_last_generation_stays(tmp_path):
    uid = await make_user(f"i-{uuid.uuid4().hex[:6]}@example.com", ["admin"])
    bad = text_of(extra="steps:\n  - {tool: k8s_list_pods, args: {nonsense: 1}}\n")
    write(tmp_path, "demo-skill", bad)
    r = await registry.scan(extra_dir=str(tmp_path))
    assert any("no argument nonsense" in e["error"] for e in r["rejected"])
    before = await active()
    sk = skills.load(str(tmp_path)).skills["demo-skill"]
    async with session_scope() as db:
        with pytest.raises(registry.PublishError):
            await registry.publish(db, sk.digest, uid)
    assert (await active()).generation == before.generation


async def test_a_duplicate_detection_rule_is_refused_at_publication(tmp_path):
    uid = await make_user(f"d-{uuid.uuid4().hex[:6]}@example.com", ["admin"])
    taken = next(r.id for sk in skills.library().skills.values() for r in sk.meta.detection)
    content = text_of(name="another-skill", rule=taken)
    sk = skills.parse(content, "t", "custom")
    assert any("also declared by" in m for m in errors(validate(sk, skills.library().skills)))
    async with session_scope() as db:  # even if it reached the table
        db.add(SkillVersion(digest=sk.digest, skill_id=sk.name, version="1", source="custom", content=content, state="validated"))
    before = await active()
    async with session_scope() as db:
        with pytest.raises(registry.PublishError, match="declared by both"):
            await registry.publish(db, sk.digest, uid)
    assert (await active()).generation == before.generation


async def test_replacing_a_builtin_is_explicit_twice(tmp_path):
    write(tmp_path, "crashloop-triage", text_of(name="crashloop-triage"))
    assert "would replace a built-in" in skills.load(str(tmp_path)).errors[0]["error"]
    write(tmp_path, "crashloop-triage", text_of(name="crashloop-triage", version="9", overrides="overrides_builtin: true\n"))
    await registry.scan(extra_dir=str(tmp_path))
    sk = skills.load(str(tmp_path)).skills["crashloop-triage"]
    uid = await make_user(f"o-{uuid.uuid4().hex[:6]}@example.com", ["admin"])
    async with session_scope() as db:
        with pytest.raises(registry.PublishError, match="allow overriding"):
            await registry.publish(db, sk.digest, uid)
        db.add(SkillPolicy(name="crashloop-triage", enabled=True, allow_override=True))
        await db.flush()
        await registry.publish(db, sk.digest, uid)


async def test_a_process_keeps_the_last_valid_generation_when_a_new_one_cannot_load():
    good = skills.library().generation
    async with session_scope() as db:
        current = await registry._active(db)
        manifest = dict(current.manifest)
        manifest["crashloop-triage"] = "0" * 64
        current.status = "retired"
        db.add(SkillGeneration(digest="x", manifest=manifest, status="active"))
    lib = await registry.refresh(force=True)
    assert lib.generation == good and lib.error and "crashloop-triage" in lib.skills
    assert registry.snapshot()["error"]


async def test_processes_report_the_generation_they_run(client):
    from moks import versions

    email = f"g-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(email, ["admin"])
    h = await login(client, email)
    async with session_scope() as db:
        await db.execute(text("DELETE FROM component_versions"))
    await versions.report("worker")
    r = (await client.get("/moks/api/skills/generation", headers=h)).json()
    assert r["consistent"] and r["processes"][0]["generation"] == r["active"]["generation"]
    async with session_scope() as db:
        await db.execute(text("UPDATE component_versions SET skill_generation = skill_generation - 1"))
    r = (await client.get("/moks/api/skills/generation", headers=h)).json()
    assert not r["consistent"] and r["processes"][0]["current"] is False


# ---- one policy --------------------------------------------------------------------------------


async def _disable(name, environments=None, enabled=False):
    async with session_scope() as db:
        await db.merge(SkillPolicy(name=name, enabled=enabled, environments=environments or []))


async def test_policy_answers_for_state_enablement_and_environment():
    sk = skills.library().skills["crashloop-triage"]
    assert registry.allowed(sk, "prod", registry.Policy()) == (True, None)
    assert "disabled" in registry.allowed(sk, "prod", registry.Policy(off={sk.name}))[1]
    assert "limited by policy" in registry.allowed(sk, "prod", registry.Policy(environments={sk.name: ["dev-*"]}))[1]
    draft = skills.parse(text_of(), "t", "custom")
    draft.state = "validated"
    assert "not published" in registry.allowed(draft, "prod", registry.Policy())[1]
    scoped = skills.parse(text_of().replace("triggers:", "environments: [dev-*]\ntriggers:"), "t", "custom")
    assert "does not apply" in registry.allowed(scoped, "prod", registry.Policy())[1]


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.setitem(conn_registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    conn_registry._cache.clear()
    yield
    conn_registry._cache.clear()


async def _detect_env():
    name = f"sk-{uuid.uuid4().hex[:6]}"
    eid = await make_env(name, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    async with session_scope() as db:
        (await db.get(Environment, eid)).detection_enabled = True
    return name, eid


async def test_disabling_a_skill_stops_its_detection_rules_and_dry_runs(cluster, client):
    name, eid = await _detect_env()
    from moks.api.environments import sync_automation_binding

    async with session_scope() as db:
        await sync_automation_binding(db, name, True)
    await _disable("crashloop-triage")
    out = await detection.tick(eid)
    assert out["blocked"] >= 1
    async with session_scope() as db:
        runs = (await db.execute(select(DetectionRun).where(DetectionRun.environment_id == eid))).scalars().all()
    assert "pod-crashloop" not in {r.rule_id for r in runs}
    assert runs and all(r.skill_digest and r.skill_generation for r in runs)  # the rest ran, pinned
    rule = next(r for r in detection.all_rules() if r.id == "pod-crashloop")
    async with session_scope() as db:
        env = await db.get(Environment, eid)
        preview = await detection.dry_run(db, env, rule, None, None)
    assert preview["status"] == "blocked" and "disabled" in preview["reason"]


async def _investigate(client, question, **body):
    name = f"inv-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    await make_user(f"{name}@example.com", [f"investigator:{name}:*"])
    h = await login(client, f"{name}@example.com")
    inv = (await client.post("/moks/api/investigations", json={"question": question, "environment": name, "namespace": "shop", **body}, headers=h)).json()
    return uuid.UUID(inv["id"])


async def _report(iid):
    async with session_scope() as db:
        return await db.get(Investigation, iid)


async def test_a_run_records_what_each_skill_did_and_pins_the_version(cluster, client):
    iid = await _investigate(client, "why is payments crashing")
    await Investigator(iid, "t").run()
    inv = await _report(iid)
    run = next(r for r in inv.report["skill_runs"] if r["skill"] == "crashloop-triage")
    assert run["digest"] == skills.library().skills["crashloop-triage"].digest and run["version"]
    assert inv.skill_generation == skills.library().generation and inv.skill_pins
    assert any(s["status"] == "executed" and s["evidence"] for s in run["steps"]) and run["reasons"]
    assert all(s["status"] == "executed" or s["reason"] for s in run["steps"])  # nothing skipped silently


async def test_a_disabled_skill_is_not_selected_and_proposes_nothing(cluster, client):
    await _disable("crashloop-triage")
    iid = await _investigate(client, "why is payments crashing")
    await Investigator(iid, "t").run()
    rep = (await _report(iid)).report
    assert "crashloop-triage" not in [s["name"] for s in rep["skills"]]
    assert all(a["skill"] != "crashloop-triage" for a in rep["skill_actions"])
    assert any(r["skill"] == "crashloop-triage" for r in rep["skill_rejected"])


async def test_disabling_during_a_run_stops_remaining_steps_and_is_recorded(cluster, client, monkeypatch):
    iid = await _investigate(client, "why is payments crashing")
    real = Investigator._call
    done = {"n": 0}

    async def call(self, executor, tool, args, purpose=None):
        out = await real(self, executor, tool, args, purpose)
        if purpose and purpose.startswith("[crashloop-triage]"):
            done["n"] += 1
            await _disable("crashloop-triage")
        return out
    monkeypatch.setattr(Investigator, "_call", call)
    await Investigator(iid, "t").run()
    rep = (await _report(iid)).report
    run = next(r for r in rep["skill_runs"] if r["skill"] == "crashloop-triage")
    assert run["stopped"] and done["n"] == 1
    assert any("skill stopped" in (s["reason"] or "") for s in run["steps"])
    assert all(a["skill"] != "crashloop-triage" for a in rep["skill_actions"])


async def test_an_operator_can_choose_a_skill_and_is_told_when_it_cannot_run(cluster, client):
    iid = await _investigate(client, "take a look at this", skill="crashloop-triage")
    await Investigator(iid, "t").run()
    assert (await _report(iid)).report["skills"][0]["reasons"][0] == "chosen by the operator"
    iid = await _investigate(client, "take a look at this", skill="cloudwatch-ingestion-gaps")
    await Investigator(iid, "t").run()
    assert any("cloudwatch-ingestion-gaps was asked for but cannot run" in n for n in (await _report(iid)).report["notes"])


def test_eligible_skills_are_filtered_before_ranking():
    q = "502 errors from the service"
    top = [s.name for s, _, _ in skills.select(q, {"service_selector_mismatch"})]
    blocked = top[0]
    after = [s.name for s, _, _ in skills.select(q, {"service_selector_mismatch"}, eligible=lambda s: s.name != blocked)]
    assert blocked not in after and after


async def test_a_document_that_looks_like_a_skill_stays_a_document():
    before = dict(skills.library().skills)
    async with session_scope() as db:
        src = KnowledgeSource(name=f"l-{uuid.uuid4().hex[:6]}", type="upload", config={}, scope_environment="x", scope_namespace="y")
        db.add(src)
        await db.flush()
        await service.index_document(db, src, "skills/evil/SKILL.md", text_of(name="crashloop-triage", version="666"), revision=None, url=None)
    await registry.refresh(force=True)
    assert skills.library().skills.keys() == before.keys() and skills.library().skills["crashloop-triage"].meta.version != "666"
    async with session_scope() as db:
        assert not (await db.execute(select(SkillVersion).where(SkillVersion.version == "666"))).first()


# ---- readiness and validation ------------------------------------------------------------------


def _skill(extra=""):
    return skills.parse(text_of(extra=extra), "t", "custom")


def test_readiness_has_four_answers_and_unknown_is_not_a_pass():
    needs_metric = _skill("telemetry: [up]\nsteps:\n  - {tool: k8s_list_pods, args: {namespace: '{namespace}'}}\n")
    assert rd.readiness(needs_metric, {"kubernetes"}, {"up"}, True)["state"] == "ready"
    unknown = rd.readiness(needs_metric, {"kubernetes"}, None)
    assert unknown["state"] == "unknown" and unknown["available"] and "could not check up" in unknown["reasons"]
    assert rd.readiness(needs_metric, {"kubernetes"}, {"other"}, True)["state"] == "unavailable"
    assert rd.readiness(needs_metric, {"kubernetes"}, {"other"}, False)["state"] == "unknown"  # a capped list proves nothing is absent
    partial = _skill("steps:\n  - {tool: k8s_list_pods, args: {namespace: '{namespace}'}}\n  - {tool: prom_targets}\n")
    assert rd.readiness(partial, {"kubernetes"})["state"] == "partial"
    assert rd.readiness(partial, set())["state"] == "unavailable"
    required = _skill("steps:\n  - {tool: k8s_list_pods, args: {namespace: '{namespace}'}}\n  - {tool: prom_targets, required: true}\n")
    assert rd.readiness(required, {"kubernetes"})["state"] == "unavailable"
    perm = _skill("permissions: [metrics.raw_query]\n")
    assert rd.readiness(perm, set(), can=lambda p: False)["state"] == "unavailable"


@pytest.mark.parametrize("extra,message", [
    ("steps:\n  - {tool: k8s_list_pods, args: {wrong: 1}}\n", "no argument wrong"),
    ("steps:\n  - {tool: k8s_list_pods}\n", "needs argument namespace"),
    ("steps:\n  - {tool: k8s_pod_logs, args: {namespace: a, pod: b, tail_lines: lots}}\n", "tail_lines"),
    ("steps:\n  - {tool: k8s_list_pods, args: {namespace: a}, when_question: '('}\n", "not a valid regular expression"),
    ("remediation:\n  - {when: crashloop, action: scale_deployment, params: {bogus: 1}}\n", "no parameter bogus"),
    ("remediation:\n  - {when: crashloop, action: restart_deployment, target: '{invented}'}\n", "unknown template"),
    ("evidence_required:\n  - {tool: no_such_tool}\n", "unknown tool"),
    ("verification:\n  - {finding: no_such_finding}\n", "no detector"),
    ("detection:\n  - {id: x-rule, kind: prometheus_alert, query: '('}\n", "not a valid regular expression"),
    ("detection:\n  - {id: x-rule, kind: finding, finding: no_such_finding}\n", "no detector"),
])
def test_publication_validation_catches(extra, message):
    assert any(message in m for m in errors(validate(_skill(extra))))


def test_template_variables_are_checked_through_lists_and_dictionaries():
    with pytest.raises(ValueError, match="unknown template"):
        _skill("steps:\n  - {tool: k8s_list_pods, args: {namespace: a, deep: {inner: ['{invented}']}}}\n")
    assert skills.render({"a": ["{namespace}", {"b": "{pod}"}]}, {"namespace": "n", "pod": "p"}) == {"a": ["n", {"b": "p"}]}
    assert skills.render(["{pod}"], {"namespace": "n"}) is None


def test_every_builtin_passes_publication_validation():
    lib = skills.load("")
    assert not lib.errors
    assert not {sk.name: errors(validate(sk, lib.skills)) for sk in lib.skills.values() if errors(validate(sk, lib.skills))}


async def test_structured_checks_are_enforced_and_plain_text_is_labeled(cluster, client):
    write_dir = Path(skills.BUILTIN) / "crashloop-triage" / "SKILL.md"
    assert write_dir.exists()
    iid = await _investigate(client, "why is payments crashing")
    await Investigator(iid, "t").run()
    run = next(r for r in (await _report(iid)).report["skill_runs"] if r["skill"] == "crashloop-triage")
    assert any(c["status"] in ("passed", "failed") for c in run["checks"])
    labels = {c["status"] for r in (await _report(iid)).report["skill_runs"] for c in r["checks"]}
    assert labels <= {"passed", "failed", "guidance_only", "not_applicable"}


def test_the_documented_example_skill_validates_and_its_case_runs():
    from moks.evals import gates
    import yaml

    root = Path(__file__).parents[2]
    sk = skills.parse((root / "docs/examples/checkout-502-triage/SKILL.md").read_text(), "example", "custom")
    assert not errors(validate(sk, skills.load("").skills))
    fx = yaml.safe_load((Path(gates.__file__).parent / "datasets/skills.yaml").read_text())["fixtures"][sk.meta.evaluation[0].fixture]
    planned = [p["tool"] for p in gates._plan(sk, fx["findings"], "payments")]
    assert all(t in planned for t in sk.meta.evaluation[0].expect.tools)
