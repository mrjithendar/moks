"""Learning records: trustworthy data to evaluate and later train on, collected without training on anything.

A record is written when an investigation finishes and when an incident's postmortem is approved. It holds
identifiers, kinds and normalized signatures, never evidence text: what the run was about (environment,
namespace, service, resource, window, recent changes), which passages were retrieved, eligible, supplied and
cited, which skills were eligible, recommended and executed with their versions and outcomes, and the model,
feature-schema and pipeline versions involved.

What a record does not say on its own:
  - A completed investigation is not a resolved incident. `outcome.investigation_status` and
    `outcome.incident_status` are kept apart, and resolution is `unknown` until someone confirms it.
  - An executed skill is not a successful one. `skills.executed[].outcome` says whether its steps ran, and
    whether it helped is a judgment someone records.
Labels (confirmed cause, resolution, verification) come only from a reviewer (`review`) or an approved postmortem
(`capture_incident`). Datasets read approved records only, drop duplicates, and keep every record of one
incident, deployment or document family in one split. Evaluation-realm records never mix with operational ones.
Nothing here trains a model.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import Incident, Investigation, LearningDataset, LearningDatasetItem, LearningJudgment, LearningRecord
from ..redaction import redact, redact_obj

log = logging.getLogger("moks.ml.learning")

SCHEMA_VERSION = 1
FEATURE_SCHEMA = "incident-signature/1"
SIGNATURE_TOKENS = 40
VERDICTS = {
    "passage": ("relevant", "irrelevant", "outdated"),
    "similar_incident": ("similar", "not_similar", "unsure"),
    "skill_recommendation": ("helpful", "not_helpful", "unsure"),
    "anomaly_observation": ("true_positive", "false_positive", "expected", "unsure"),
}
PURPOSES = ("incident_similarity", "skill_ranking", "retrieval", "anomaly")

_VOLATILE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), " <uuid> "),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), " <ip> "),
    (re.compile(r"\b(?:0x)?[0-9a-f]{7,64}\b"), " <hex> "),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|mi|gi|ki|%)?\b"), " <n> "),
    (re.compile(r"(?:/[\w.-]+){2,}"), " <path> "),
]
_TOKEN = re.compile(r"[a-z][a-z0-9_]{2,}|<[a-z]+>")
_STOP = set("the and for with from that this was are has have not but into after before then than when while error errors".split())


def signature(text: str) -> list[str]:
    """An error message reduced to what recurs across incidents: identifiers, numbers, addresses and paths
    become placeholders, so two OOMs on different pods share a signature and a secret never survives."""
    t = redact(str(text or "")).lower()
    for rx, sub in _VOLATILE:
        t = rx.sub(sub, t)
    seen: dict[str, None] = {}
    for tok in _TOKEN.findall(t):
        if tok not in _STOP:
            seen.setdefault(tok, None)
    return list(seen)[:SIGNATURE_TOKENS]


def _workload(resource: str) -> str:
    kind, _, name = str(resource).partition("/")
    parts = name.split("-")
    if kind.lower() == "pod" and len(parts) >= 3:
        return "-".join(parts[:-2])
    return name


def features(report: dict, question: str = "") -> dict[str, Any]:
    """The comparable shape of an investigation, from MOKS's own findings: never from retrieved text."""
    findings = report.get("findings") or []
    symptoms = sorted({str(f.get("kind")) for f in findings if f.get("kind") and f.get("kind") != "recent_rollout"})
    kinds = sorted({str(r).partition("/")[0].lower() for f in findings for r in f.get("resources") or [] if "/" in str(r)})
    services = sorted({_workload(r) for f in findings for r in f.get("resources") or [] if _workload(r)} | {str(f["controller"]).partition("/")[2] for f in findings if f.get("controller")})
    targets = [t.get("mention") for t in (report.get("request") or {}).get("targets") or [] if isinstance(t, dict) and t.get("kind") != "object" and t.get("mention")]
    target = str(targets[0]).lower() if targets else None
    sig: list[str] = []
    for f in findings:
        if f.get("kind") in ("crash_log_reason", "log_errors", "config_error", "image_pull"):
            sig += signature(f"{f.get('title', '')} {f.get('detail', '')}")
    changes = [{"kind": "rollout", "resource": (f.get("resources") or [None])[0], "namespace": f.get("namespace"), "at": f.get("at") or f.get("observed_at")}
               for f in findings if f.get("kind") == "recent_rollout"][:10]
    severity = sorted({str(f.get("severity")) for f in findings if f.get("severity")})
    return {"symptoms": symptoms, "resource_kinds": kinds, "services": sorted(set(services) | ({target} if target else set()))[:20],
            "signature": list(dict.fromkeys(sig))[:SIGNATURE_TOKENS], "changes": changes, "severities": severity,
            "question_signature": signature(question)[:20]}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _content_hash(feat: dict, label: dict) -> str:
    """Two records with the same features and label are one example, whatever their ids."""
    return _hash({"f": {k: feat.get(k) for k in ("symptoms", "resource_kinds", "services", "signature")}, "l": {k: label.get(k) for k in ("confirmed_cause", "resolution")}})


def _expiry(now: datetime) -> datetime | None:
    days = get_settings().ml_learning_retention_days
    return now + timedelta(days=days) if days > 0 else None


def retrieval_of(knowledge: dict) -> dict[str, Any]:
    passages = knowledge.get("passages") or []

    def ids(state_ok) -> list[str]:
        return [p.get("id") or f"{p.get('ref')}.{p.get('rank')}" for p in passages if state_ok(p)]
    return {"retrieved": ids(lambda p: True), "eligible": ids(lambda p: p.get("authority") in ("target", "dependency", "shared", "unverified")),
            "supplied": ids(lambda p: p.get("provided")), "cited": ids(lambda p: p.get("cited") or p.get("used")),
            "passages": [{"id": p.get("id"), "chunk_id": p.get("chunk_id"), "document_id": p.get("document_id"), "revision": p.get("revision"), "lines": p.get("lines"),
                          "authority": p.get("authority"), "state": p.get("state")} for p in passages][:60],
            "status": knowledge.get("status")}


def skills_of(report: dict, eligible: list[str] | None, requested: str | None) -> dict[str, Any]:
    runs = report.get("skill_runs") or []

    def outcome(run: dict) -> str:
        steps = run.get("steps") or []
        if run.get("not_run"):
            return "not_run"
        if run.get("stopped"):
            return "stopped"
        ran = [s for s in steps if s.get("status") in ("executed", "failed")]
        if not ran:
            return "not_run"
        return "steps_failed" if any(s.get("status") == "failed" for s in ran) else "steps_ran"
    rec = report.get("skill_recommendations") or {}
    return {"eligible": sorted(eligible or []), "operator_selected": requested,
            "recommended": [{"skill": s.get("name"), "version": s.get("version"), "reasons": s.get("reasons")} for s in report.get("skills") or []],
            "semantic": {"mode": rec.get("mode"), "ranked": [{"skill": c.get("skill"), "score": c.get("score")} for c in rec.get("ranked") or []][:10], "added": rec.get("added") or []},
            "executed": [{"skill": r.get("skill"), "version": r.get("version"), "digest": r.get("digest"), "outcome": outcome(r),
                          "note": "steps ran; whether the skill helped is recorded only by a judgment"} for r in runs]}


async def capture(db: AsyncSession, inv_row: Investigation, env_name: str, report: dict, *, knowledge: dict, eligible: list[str] | None, versions: dict) -> LearningRecord | None:
    """Writes or refreshes the investigation's record. Idempotent: one record per investigation."""
    if not get_settings().ml_learning_capture:
        return None
    now = datetime.now(timezone.utc)
    feat = features(report, inv_row.question)
    resource = str(report.get("primary_resource") or "")[:300] or None
    incident = await db.get(Incident, inv_row.incident_id) if inv_row.incident_id else None
    values = dict(
        schema_version=SCHEMA_VERSION, kind="investigation", investigation_id=inv_row.id, incident_id=inv_row.incident_id, environment=env_name, namespace=inv_row.namespace,
        service=(feat["services"] or [None])[0], resource=resource, realm="evaluation" if inv_row.trigger == "eval" else "operational",
        event_from=inv_row.time_from, event_to=inv_row.time_to, features=feat, retrieval=retrieval_of(knowledge), skills=skills_of(report, eligible, inv_row.requested_skill),
        outcome={"investigation_status": "completed", "incident_status": incident.status if incident else None, "resolution": "unknown",
                 "note": "a completed investigation is not a resolved incident"},
        versions={**{k: v for k, v in (versions or {}).items() if isinstance(v, (str, int, float, type(None)))}, "feature_schema": FEATURE_SCHEMA, "record_schema": SCHEMA_VERSION,
                  "knowledge": (report.get("knowledge") or {}).get("version"), "llm": report.get("generated_by")},
        fingerprint=_hash(["investigation", str(inv_row.id)]), updated_at=now, expires_at=_expiry(now))
    rec = (await db.execute(select(LearningRecord).where(LearningRecord.fingerprint == values["fingerprint"]))).scalar_one_or_none()
    if rec is None:
        rec = LearningRecord(**values, content_hash=_content_hash(feat, {}))
        db.add(rec)
    else:  # a re-run keeps any label, and the resolution, a person already gave
        if rec.label_state != "unreviewed":
            values["outcome"] = {**values["outcome"], "resolution": (rec.outcome or {}).get("resolution", "unknown")}
        for k, v in values.items():
            setattr(rec, k, v)
        rec.content_hash = _content_hash(feat, rec.label or {})
    await db.flush()
    return rec


async def capture_incident(db: AsyncSession, inc: Incident, env_name: str, approved_by: uuid.UUID | None) -> LearningRecord:
    """An approved postmortem is a reviewed outcome: the incident's record takes its cause, resolution and
    whether the cause was verified, and the features of every investigation linked to it."""
    now = datetime.now(timezone.utc)
    linked = (await db.execute(select(LearningRecord).where(LearningRecord.kind == "investigation", LearningRecord.incident_id == inc.id))).scalars().all()
    merged: dict[str, set] = {k: set() for k in ("symptoms", "resource_kinds", "services", "signature")}
    changes: list[dict] = []
    for r in linked:
        for k in merged:
            merged[k] |= set(r.features.get(k) or [])
        changes += r.features.get("changes") or []
    pm = inc.postmortem or {}
    merged["signature"] |= set(signature(f"{inc.title} {pm.get('root_cause', '')}"))
    if inc.resource:
        merged["services"].add(_workload(inc.resource))
        merged["resource_kinds"].add(inc.resource.partition("/")[0].lower())
    feat = {k: sorted(v)[: SIGNATURE_TOKENS if k == "signature" else 20] for k, v in merged.items()} | {"changes": changes[:10]}
    label = {"confirmed_cause": redact(pm.get("root_cause") or "")[:2000], "resolution": redact(pm.get("resolution") or "")[:2000],
             "verification": "verified" if pm.get("root_cause_verified") else "unverified", "source": "approved_postmortem"}
    values = dict(schema_version=SCHEMA_VERSION, kind="incident", investigation_id=None, incident_id=inc.id, environment=env_name, namespace=inc.namespace,
                  service=(sorted(merged["services"]) or [None])[0], resource=inc.resource, realm="operational", event_from=inc.first_observed_at or inc.created_at,
                  event_to=inc.resolved_at, features=feat, retrieval={}, skills={"investigations": [str(r.investigation_id) for r in linked][:20]},
                  outcome={"incident_status": inc.status, "resolution": "resolved" if inc.status in ("resolved", "closed") else "unknown", "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None},
                  label=label, label_state="approved", reviewed_by=approved_by, reviewed_at=now, versions={"feature_schema": FEATURE_SCHEMA, "record_schema": SCHEMA_VERSION},
                  fingerprint=_hash(["incident", str(inc.id)]), content_hash=_content_hash(feat, label), updated_at=now, expires_at=None)  # an approved incident is kept until deleted
    rec = (await db.execute(select(LearningRecord).where(LearningRecord.fingerprint == values["fingerprint"]))).scalar_one_or_none()
    if rec is None:
        rec = LearningRecord(**values)
        db.add(rec)
    else:
        for k, v in values.items():
            setattr(rec, k, v)
    await db.flush()
    return rec


async def review(db: AsyncSession, rec: LearningRecord, reviewer: uuid.UUID, *, approve: bool, confirmed_cause: str = "", resolution: str = "", verification: str = "unverified", note: str = "") -> LearningRecord:
    """A person confirms (or rejects) what happened. This is the only way an investigation record gets a label."""
    if verification not in ("verified", "unverified", "failed"):
        raise ValueError("verification must be verified, unverified or failed")
    if approve and not confirmed_cause.strip():
        raise ValueError("approving a record needs the confirmed cause")
    rec.label = {"confirmed_cause": redact(confirmed_cause)[:2000], "resolution": redact(resolution)[:2000], "verification": verification, "note": redact(note)[:1000], "source": "reviewer"} if approve else {"note": redact(note)[:1000], "source": "reviewer"}
    rec.label_state = "approved" if approve else "rejected"
    rec.reviewed_by, rec.reviewed_at = reviewer, datetime.now(timezone.utc)
    rec.content_hash = _content_hash(rec.features or {}, rec.label)
    rec.outcome = {**(rec.outcome or {}), "resolution": "confirmed" if approve and resolution.strip() else (rec.outcome or {}).get("resolution", "unknown")}
    await db.flush()
    return rec


async def judge(db: AsyncSession, user_id: uuid.UUID, *, target_kind: str, target_id: str, verdict: str, note: str = "", context_id: str = "",
                environment: str | None = None, namespace: str | None = None, record_id: uuid.UUID | None = None) -> LearningJudgment:
    """One verdict per person per target and context; a second submission replaces the first. Open until reviewed."""
    if target_kind not in VERDICTS or verdict not in VERDICTS[target_kind]:
        raise ValueError(f"verdict for {target_kind} must be one of {', '.join(VERDICTS.get(target_kind, ()))}")
    row = (await db.execute(select(LearningJudgment).where(LearningJudgment.created_by == user_id, LearningJudgment.target_kind == target_kind,
                                                           LearningJudgment.target_id == target_id, LearningJudgment.context_id == context_id))).scalar_one_or_none()
    if row is None:
        row = LearningJudgment(created_by=user_id, target_kind=target_kind, target_id=target_id[:200], context_id=context_id[:64])
        db.add(row)
    row.verdict, row.note, row.environment, row.namespace, row.record_id = verdict, redact(note)[:2000], environment, namespace, record_id
    row.state, row.reviewed_by, row.reviewed_at = "open", None, None  # a changed verdict needs review again
    await db.flush()
    return row


async def purge(db: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """Retention: expired records and datasets go. Approved incident records have no expiry."""
    now = now or datetime.now(timezone.utc)
    records = (await db.execute(delete(LearningRecord).where(LearningRecord.expires_at.is_not(None), LearningRecord.expires_at < now))).rowcount or 0
    datasets = (await db.execute(delete(LearningDataset).where(LearningDataset.expires_at.is_not(None), LearningDataset.expires_at < now))).rowcount or 0
    return {"records": records, "datasets": datasets}


# ---- datasets ----------------------------------------------------------------------------------------


def group_key(rec: LearningRecord) -> str:
    """What must never be split between train and evaluation: one incident, one deployment, one document family."""
    if rec.incident_id:
        return f"incident:{rec.incident_id}"
    rollout = next((c.get("resource") for c in (rec.features or {}).get("changes") or [] if c.get("resource")), None)
    if rollout:
        return f"deploy:{rec.environment}/{rec.namespace}/{rollout}"
    docs = sorted({p.get("document_id") for p in (rec.retrieval or {}).get("passages") or [] if p.get("document_id") and p.get("state") == "cited"})
    if docs:
        return f"docs:{_hash(docs)[:16]}"
    return f"record:{rec.id}"


def split(records: list[LearningRecord], *, method: str = "grouped", eval_fraction: float = 0.2, seed: int = 7) -> tuple[dict[uuid.UUID, str], dict[str, Any]]:
    """train or eval for each record. Duplicates (same content hash) are dropped first, keeping the earliest;
    then whole groups go to one side. `chronological` puts the latest groups in eval, for telemetry and for
    anything whose future must not leak into the past."""
    if method not in ("grouped", "chronological"):
        raise ValueError("split method must be grouped or chronological")
    if not 0 < eval_fraction < 1:
        raise ValueError("eval_fraction must be between 0 and 1")
    ordered = sorted(records, key=lambda r: (r.event_from or r.created_at or datetime.min.replace(tzinfo=timezone.utc), str(r.id)))
    kept, seen, dropped = [], set(), 0
    for r in ordered:
        if r.content_hash in seen:
            dropped += 1
            continue
        seen.add(r.content_hash)
        kept.append(r)
    groups: dict[str, list[LearningRecord]] = {}
    for r in kept:
        groups.setdefault(group_key(r), []).append(r)
    names = list(groups)
    if method == "chronological":
        names.sort(key=lambda g: min((r.event_from or r.created_at) for r in groups[g]))
        cut = len(names) - max(1, round(len(names) * eval_fraction)) if len(names) > 1 else len(names)
        evals = set(names[cut:])
    else:
        rng = random.Random(seed)
        names.sort()
        rng.shuffle(names)
        evals = set(names[: max(1, round(len(names) * eval_fraction))]) if len(names) > 1 else set()
    out = {r.id: ("eval" if g in evals else "train") for g in names for r in groups[g]}
    return out, {"method": method, "eval_fraction": eval_fraction, "seed": seed, "duplicates_dropped": dropped, "groups": len(names), "eval_groups": len(evals)}


def check_leakage(items: Iterable[tuple[str, str, str]]) -> list[str]:
    """(split, group_key, content_hash) rows. Returns every way train and eval share something."""
    sides: dict[str, dict[str, set]] = {"group": {}, "content": {}}
    for sp, g, h in items:
        sides["group"].setdefault(g, set()).add(sp)
        sides["content"].setdefault(h, set()).add(sp)
    return [f"{kind} {key} is in both train and eval" for kind, m in sides.items() for key, s in m.items() if len(s) > 1]


def eligible_for(purpose: str):
    """Which records may enter a dataset for this purpose. Supervised purposes need an approved label."""
    if purpose == "incident_similarity":
        return lambda r: r.label_state == "approved" and r.kind == "incident"
    if purpose in ("skill_ranking", "retrieval", "anomaly"):
        return lambda r: r.label_state == "approved"
    raise ValueError(f"purpose must be one of {', '.join(PURPOSES)}")


async def build_dataset(db: AsyncSession, *, name: str, purpose: str, created_by: uuid.UUID | None, realm: str = "operational", environment: str | None = None,
                        method: str = "grouped", eval_fraction: float = 0.2, seed: int = 7, retention_days: int | None = 180) -> LearningDataset:
    """A frozen, versioned selection with its split. Refuses to save one that leaks."""
    if realm not in ("operational", "evaluation"):
        raise ValueError("realm must be operational or evaluation")
    ok = eligible_for(purpose)
    q = select(LearningRecord).where(LearningRecord.realm == realm, LearningRecord.label_state == "approved")
    if environment:
        q = q.where(LearningRecord.environment == environment)
    records = [r for r in (await db.execute(q.order_by(LearningRecord.created_at).limit(20000))).scalars().all() if ok(r)]
    if not records:
        raise ValueError("no approved records match; a dataset is built from reviewed outcomes only")
    sides, info = split(records, method=method, eval_fraction=eval_fraction, seed=seed)
    by_id = {r.id: r for r in records}
    rows = [(sides[i], group_key(by_id[i]), by_id[i].content_hash) for i in sides]
    leaks = check_leakage(rows)
    if leaks:
        raise ValueError("dataset would leak: " + "; ".join(leaks[:3]))
    version = int((await db.execute(select(func.coalesce(func.max(LearningDataset.version), 0)).where(LearningDataset.name == name))).scalar_one()) + 1
    checksum = _hash(sorted([str(i), s] for i, s in sides.items()))
    now = datetime.now(timezone.utc)
    ds = LearningDataset(name=name[:100], version=version, purpose=purpose, realm=realm, feature_schema=FEATURE_SCHEMA, filters={"environment": environment},
                         split=info, counts={"records": len(sides), "train": sum(1 for s in sides.values() if s == "train"), "eval": sum(1 for s in sides.values() if s == "eval")},
                         checksum=checksum, created_by=created_by, expires_at=now + timedelta(days=retention_days) if retention_days else None)
    db.add(ds)
    await db.flush()
    db.add_all([LearningDatasetItem(dataset_id=ds.id, record_id=i, split=s, group_key=group_key(by_id[i])[:200]) for i, s in sides.items()])
    await db.flush()
    return ds


EXPORT_FIELDS = ("id", "kind", "environment", "namespace", "service", "resource", "features", "label", "outcome", "versions")


async def export_rows(db: AsyncSession, ds: LearningDataset) -> list[dict[str, Any]]:
    """Features and labels only, already redacted at capture and redacted again here. No evidence text, no
    passage text, no user identities."""
    rows = (await db.execute(select(LearningDatasetItem, LearningRecord).join(LearningRecord, LearningRecord.id == LearningDatasetItem.record_id)
                             .where(LearningDatasetItem.dataset_id == ds.id).order_by(LearningDatasetItem.split, LearningRecord.created_at))).all()
    out = []
    for item, rec in rows:
        body = {k: getattr(rec, k) for k in EXPORT_FIELDS}
        body["id"] = str(body["id"])
        body = redact_obj(json.loads(json.dumps(body, default=str)))
        out.append({"split": item.split, "group": item.group_key, "dataset": f"{ds.name}@{ds.version}", "record": body})
    return out


def record_out(r: LearningRecord) -> dict[str, Any]:
    return {"id": str(r.id), "kind": r.kind, "investigation_id": str(r.investigation_id) if r.investigation_id else None, "incident_id": str(r.incident_id) if r.incident_id else None,
            "environment": r.environment, "namespace": r.namespace, "service": r.service, "resource": r.resource, "realm": r.realm,
            "event_from": r.event_from.isoformat() if r.event_from else None, "event_to": r.event_to.isoformat() if r.event_to else None,
            "features": r.features, "retrieval": r.retrieval, "skills": r.skills, "outcome": r.outcome, "label": r.label, "label_state": r.label_state,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None, "versions": r.versions, "created_at": r.created_at.isoformat() if r.created_at else None,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None}
