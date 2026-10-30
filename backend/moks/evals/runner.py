"""Evaluation runner: replays dataset cases through the real orchestrator (policy-checked
executor, detectors, report validation, and the active LLM in `llm` mode) against fixture
connectors, then scores each report.

Score per case = mean of the checks that apply: expected finding kinds present, required fact
phrases present, forbidden phrases absent, every fact cites existing evidence, no causes marked
verified, high-severity false positives within the limit, investigation completed.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from ..db import session_scope
from ..models import AUTOMATION_USER_ID, Environment, EvalRun, Evidence, Investigation, RoleBinding
from ..orchestrator.engine import Investigator
from .fixture import FixtureKubernetes

DATASETS = Path(__file__).parent / "datasets"


def list_datasets() -> list[dict]:
    out = []
    for p in sorted(DATASETS.glob("*.yaml")):
        d = yaml.safe_load(p.read_text())
        out.append({"name": d["name"], "cases": len(d["cases"])})
    return out


def load(name: str) -> dict:
    p = DATASETS / f"{name}.yaml"
    if not p.exists() or "/" in name:
        raise ValueError(f"unknown dataset {name!r}")
    return yaml.safe_load(p.read_text())


async def _eval_env(name: str) -> uuid.UUID:
    async with session_scope() as db:
        env = (await db.execute(select(Environment).where(Environment.name == name))).scalar_one_or_none()
        if env is None:
            env = Environment(name=name, display_name="Evaluation (fixtures)", kind="minikube", allowed_namespaces=["*"],
                              denied_namespaces=[], max_query_window_minutes=360)
            db.add(env)
            await db.flush()
        if not (await db.execute(select(RoleBinding).where(RoleBinding.user_id == AUTOMATION_USER_ID, RoleBinding.environment == "eval-*"))).first():
            db.add(RoleBinding(user_id=AUTOMATION_USER_ID, role="investigator", environment="eval-*", namespace="*", source="automation"))
        return env.id


def score_case(case: dict, inv: Investigation, evidence_refs: set[str]) -> dict:
    exp = case.get("expect", {})
    rep = inv.report or {}
    text = json.dumps(rep).lower()
    kinds = {f["kind"] for f in rep.get("findings", [])}
    checks: dict[str, bool] = {"completed": inv.status == "completed"}
    if exp.get("finding_kinds"):
        checks["expected_findings"] = all(k in kinds for k in exp["finding_kinds"])
    if exp.get("fact_contains"):
        facts = " ".join(f["statement"] for f in rep.get("observed_facts", [])).lower()
        checks["fact_phrases"] = all(p.lower() in facts for p in exp["fact_contains"])
    if exp.get("forbid"):
        checks["no_forbidden_text"] = not any(p.lower() in text for p in exp["forbid"])
    checks["citations_valid"] = all(f.get("evidence") and set(f["evidence"]) <= evidence_refs for f in rep.get("observed_facts", []))
    checks["nothing_marked_verified"] = not any(c.get("verified") for c in rep.get("suspected_causes", []))
    if "max_high_findings" in exp:
        checks["no_false_positives"] = sum(1 for f in rep.get("findings", []) if f["severity"] == "high") <= exp["max_high_findings"]
    return {"case": case["id"], "score": round(sum(checks.values()) / len(checks), 4), "checks": checks,
            "findings": sorted(kinds), "mode": rep.get("generated_by"), "investigation_id": str(inv.id),
            "tool_calls": inv.tool_calls, "tokens": inv.tokens_in + inv.tokens_out}


async def start(dataset: str, mode: str, created_by=None) -> uuid.UUID:
    load(dataset)
    async with session_scope() as db:
        run = EvalRun(dataset=dataset, mode=mode, created_by=created_by)
        db.add(run)
        await db.flush()
        return run.id


async def execute(run_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as db:
        run = await db.get(EvalRun, run_id)
        dataset, mode = run.dataset, run.mode
    data = load(dataset)
    env_id = await _eval_env(f"eval-{dataset}")
    results = []
    for case in data["cases"]:
        now = datetime.now(timezone.utc)
        async with session_scope() as db:
            inv = Investigation(created_by=AUTOMATION_USER_ID, environment_id=env_id, namespace=case["namespace"], question=case["question"],
                                time_from=now - timedelta(minutes=30), time_to=now, status="running", trigger="eval")
            db.add(inv)
            await db.flush()
            inv_id = inv.id
        await Investigator(inv_id, f"eval-{run_id}", connectors_override={"kubernetes": (None, FixtureKubernetes(case))},
                           use_llm=(mode == "llm")).run()
        async with session_scope() as db:
            inv = await db.get(Investigation, inv_id)
            refs = set((await db.execute(select(Evidence.ref).where(Evidence.investigation_id == inv_id))).scalars())
            results.append(score_case(case, inv, refs))
    total = round(sum(r["score"] for r in results) / len(results), 4) if results else 0
    async with session_scope() as db:
        run = await db.get(EvalRun, run_id)
        run.results, run.score, run.status, run.finished_at = results, Decimal(str(total)), "completed", datetime.now(timezone.utc)
    return {"score": total, "cases": len(results)}
