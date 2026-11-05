"""Similar historical incidents: which approved incidents looked like this one, how, how not, and how they ended.

Candidates are incidents whose postmortem was approved (learning records of kind `incident`, label approved, in
the process's realm, so an evaluation run never sees operational incidents and the reverse). Access is decided before anything is compared: the person must be able to read knowledge and
ML results in the incident's environment and namespace, and the incident must lie inside the request's scope.

Two signals are combined:
  structure  symptoms (finding kinds), error signature tokens, service, resource kind and recent-change context,
             from MOKS's own findings and the reviewed postmortem, compared as sets
  text       the existing knowledge search, restricted to the postmortem source, which brings in embeddings
             when the active index has them and keyword search when it does not
Text is optional. If the search fails or embeddings are unavailable the structural score still works, and the
result says which signals were used.

Historical similarity is not evidence of the current cause. Every result says what matched, what differs, how
old it is, whether its cause was verified, and links to the incident. The score ranks; it is not a probability.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import Incident, KnowledgeSource, LearningRecord
from ..rbac import P, Principal
from ..rag import access

log = logging.getLogger("moks.ml.similar")

NOTE = ("Historical similarity does not show the current cause. A past incident is a lead to check against this "
        "investigation's live evidence, never a conclusion.")
WEIGHTS = {"symptoms": 0.35, "signature": 0.25, "service": 0.15, "resource": 0.10, "change": 0.05, "text": 0.10}
MIN_SCORE = 0.2
INCIDENT_PATH = re.compile(r"^incidents/([0-9a-f-]{36})\.md$")


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def compare(current: dict, past: dict) -> tuple[float, dict[str, float], dict[str, Any]]:
    """(score, parts, explanation). Missing signals score 0 and are named, never assumed to match."""
    cs, ps = set(current.get("symptoms") or []), set(past.get("symptoms") or [])
    cg, pg = set(current.get("signature") or []), set(past.get("signature") or [])
    cv, pv = set(current.get("services") or []), set(past.get("services") or [])
    ck, pk = set(current.get("resource_kinds") or []), set(past.get("resource_kinds") or [])
    cc, pc = bool(current.get("changes")), bool(past.get("changes"))
    parts = {"symptoms": _jaccard(cs, ps), "signature": _jaccard(cg, pg), "service": 1.0 if cv & pv else 0.0, "resource": _jaccard(ck, pk),
             "change": 1.0 if cc and pc else 0.0}
    score = sum(WEIGHTS[k] * v for k, v in parts.items())
    why = {
        "matching": {"symptoms": sorted(cs & ps), "signature": sorted(cg & pg)[:8], "services": sorted(cv & pv), "resource_kinds": sorted(ck & pk),
                     "change_context": "both followed a recent rollout" if cc and pc else None},
        "differences": {"only_now": sorted(cs - ps), "only_then": sorted(ps - cs), "services_then": sorted(pv - cv)[:5] if pv and not (cv & pv) else [],
                        "change_context": None if cc == pc else ("a rollout preceded this one only" if cc else "a rollout preceded the past one only")},
        "missing_signals": [k for k, (a, b) in {"symptoms": (cs, ps), "signature": (cg, pg), "services": (cv, pv)}.items() if not a or not b],
    }
    return score, parts, why


def _readable(principal: Principal, rec: LearningRecord, environment: str | None, namespace: str | None) -> bool:
    return (access.can_read(principal, rec.environment, rec.namespace) and access.in_request(environment, namespace, rec.environment, rec.namespace)
            and principal.can(P.ML_READ, rec.environment, None if rec.namespace in (None, "*") else rec.namespace))


async def _text_ranks(db: AsyncSession, principal: Principal, query: str, environment: str | None, namespace: str | None) -> tuple[dict[str, int], dict[str, Any]]:
    """Incident id -> rank from the knowledge search over approved postmortems. Never raises."""
    from ..rag import service

    if len(query.strip()) < 2:
        return {}, {"used": False, "reason": "nothing to search for"}
    src = (await db.execute(select(KnowledgeSource).where(KnowledgeSource.name == "incident-postmortems"))).scalar_one_or_none()
    if src is None:
        return {}, {"used": False, "reason": "no postmortem has been approved yet"}
    try:
        r = await service.search(db, principal, query[:500], 20, environment=environment, namespace=namespace, sources=[str(src.id)])
    except Exception as exc:  # a lead, not a dependency: structure alone still works
        log.warning("similar incident text search failed", extra={"event": "similar_text_failed", "error_type": type(exc).__name__})
        return {}, {"used": False, "reason": f"knowledge search failed ({type(exc).__name__}); structural similarity only"}
    ranks: dict[str, int] = {}
    for i, hit in enumerate(r.get("results") or []):
        m = INCIDENT_PATH.match(str((hit.get("citation") or {}).get("path") or ""))
        if m and m.group(1) not in ranks:
            ranks[m.group(1)] = i
    info = {"used": True, "mode": r.get("mode"), "fallback_reason": r.get("fallback_reason")}
    if r.get("mode") == "keyword" and r.get("fallback_reason"):
        info["note"] = "embeddings unavailable: text matching is keyword-only"
    return ranks, info


async def find(db: AsyncSession, principal: Principal, current: dict, *, environment: str | None, namespace: str | None, text: str = "",
               k: int | None = None, exclude_incident: uuid.UUID | None = None) -> dict[str, Any]:
    from . import mode

    state = await mode(db, "similar_incidents")
    if state == "disabled":
        return {"status": "disabled", "mode": state, "results": [], "note": NOTE}
    s = get_settings()
    k = min(k or s.ml_similar_incidents_k, 10)
    namespace = None if namespace in (None, "*") else namespace
    q = (select(LearningRecord).where(LearningRecord.kind == "incident", LearningRecord.label_state == "approved", LearningRecord.realm == access.realm())  # evaluation runs see only evaluation incidents
         .order_by(LearningRecord.event_from.desc().nulls_last()).limit(s.ml_similar_incidents_candidates))
    if environment:
        q = q.where(LearningRecord.environment == environment)
    rows = [r for r in (await db.execute(q)).scalars().all() if _readable(principal, r, environment, namespace) and (exclude_incident is None or r.incident_id != exclude_incident)]
    if not rows:
        return {"status": "empty", "mode": state, "results": [], "considered": 0, "note": NOTE, "reason": "no approved incident you may read in this scope"}
    query = " ".join([*(current.get("symptoms") or []), *(current.get("services") or []), *(current.get("signature") or [])[:15], text]).replace("_", " ")
    ranks, text_info = await _text_ranks(db, principal, query, environment, namespace)
    incidents = {i.id: i for i in (await db.execute(select(Incident).where(Incident.id.in_([r.incident_id for r in rows if r.incident_id])))).scalars().all()}
    now = datetime.now(timezone.utc)
    scored = []
    for r in rows:
        score, parts, why = compare(current, r.features or {})
        rank = ranks.get(str(r.incident_id))
        parts["text"] = 1.0 / (1 + rank) if rank is not None else 0.0
        score += WEIGHTS["text"] * parts["text"]
        if score < MIN_SCORE:
            continue
        inc = incidents.get(r.incident_id)
        when = r.event_from or (inc.created_at if inc else None)
        label = r.label or {}
        scored.append({
            "incident_id": str(r.incident_id) if r.incident_id else None, "record_id": str(r.id), "title": inc.title if inc else None, "environment": r.environment, "namespace": r.namespace, "service": r.service,
            "score": round(score, 3), "signals": {k2: round(v, 3) for k2, v in parts.items()},
            "matching": why["matching"], "differences": why["differences"], "missing_signals": why["missing_signals"],
            "age_days": (now - when).days if when else None, "occurred_at": when.isoformat() if when else None,
            "resolution": {"status": (r.outcome or {}).get("resolution", "unknown"), "cause": label.get("confirmed_cause", "")[:400], "resolution": label.get("resolution", "")[:400],
                           "cause_verified": label.get("verification") == "verified"},
            "citation": {"incident": str(r.incident_id), "url": f"{s.base_path.rstrip('/')}/incidents/{r.incident_id}", "document": f"incidents/{r.incident_id}.md" if rank is not None else None},
        })
    scored.sort(key=lambda x: (-x["score"], x["age_days"] if x["age_days"] is not None else 10**6, x["incident_id"]))
    return {"status": "ok" if scored else "empty", "mode": state, "results": scored[:k], "considered": len(rows), "text": text_info,
            "method": "structural (symptoms, signature, service, resource, change) plus knowledge search over approved postmortems", "note": NOTE,
            **({} if scored else {"reason": "no approved incident is similar enough"})}
