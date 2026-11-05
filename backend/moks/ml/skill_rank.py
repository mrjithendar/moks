"""Semantic skill recommendation over skills that policy already allows.

Order is fixed: publication, enablement, environment policy, connectors, telemetry and permissions decide which
skills are eligible (skills/selection.py), the deterministic ranker picks its skills (skills.select), and only
then does this rank the eligible ones by how close their metadata is to the symptoms. It never sees an
ineligible skill, so it cannot bring one back.

Two methods, chosen per call: cosine similarity of embeddings when the knowledge index has an embedding model
(skill vectors cached by digest), otherwise TF-IDF cosine over the same text. The method used is recorded.

  shadow   the ranking is recorded beside the deterministic choice; the run uses the deterministic choice
  enabled  skills above MIN_SIMILARITY that the rules did not pick may fill the remaining places; nothing the
           rules or the operator chose is removed or reordered
Any error falls back to the deterministic choice. Explanations name the shared terms, never invented reasons.
No supervised ranker is trained here: that waits for enough reviewed outcomes (docs/ML.md).
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Any, Awaitable, Callable

from ..observability import ML_FALLBACKS
from ..orchestrator.knowledge import SYMPTOM_WORDS

log = logging.getLogger("moks.ml.skill_rank")

MIN_SIMILARITY = 0.18
WORD = re.compile(r"[a-z][a-z0-9]{2,}")
STOP = set("the and for are with from that this when what which into skill check checks pods pod show".split())
_VECTORS: dict[str, list[float]] = {}  # skill digest -> embedding, for one process

Embed = Callable[[list[str]], Awaitable[list[list[float]] | None]]


def skill_text(sk: Any) -> str:
    m = sk.meta
    return " ".join([m.name.replace("-", " "), m.title or "", m.description, " ".join(m.triggers.keywords), " ".join(SYMPTOM_WORDS.get(k, k.replace("_", " ")) for k in m.triggers.findings),
                     " ".join(m.resource_types)])


def symptom_text(question: str, finding_kinds: set[str], signature: list[str] | None = None) -> str:
    return " ".join([question, *(SYMPTOM_WORDS.get(k, k.replace("_", " ")) for k in sorted(finding_kinds)), *(signature or [])[:20]])


def _terms(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP]


def _cos(a: dict[str, float] | list[float], b: dict[str, float] | list[float]) -> float:
    if isinstance(a, dict):
        dot = sum(v * b.get(k, 0.0) for k, v in a.items())
        na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    else:
        dot = sum(x * y for x, y in zip(a, b))
        na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def lexical(query: str, skills: list[Any]) -> list[tuple[Any, float, list[str]]]:
    docs = {sk.name: Counter(_terms(skill_text(sk))) for sk in skills}
    df = Counter(t for c in docs.values() for t in c)
    n = len(docs) or 1
    idf = {t: math.log((1 + n) / (1 + d)) + 1 for t, d in df.items()}
    q = Counter(_terms(query))
    qv = {t: c * idf.get(t, 0.0) for t, c in q.items() if t in idf}
    out = []
    for sk in skills:
        dv = {t: c * idf[t] for t, c in docs[sk.name].items()}
        out.append((sk, _cos(qv, dv), sorted(set(qv) & set(dv))[:6]))
    return out


async def semantic(query: str, skills: list[Any], embed: Embed) -> list[tuple[Any, float, list[str]]] | None:
    missing = [sk for sk in skills if sk.digest not in _VECTORS]
    vecs = await embed([query] + [skill_text(sk) for sk in missing])
    if not vecs or len(vecs) != len(missing) + 1:
        return None
    for sk, v in zip(missing, vecs[1:]):
        _VECTORS[sk.digest] = v
    qterms = set(_terms(query))
    return [(sk, _cos(vecs[0], _VECTORS[sk.digest]), sorted(qterms & set(_terms(skill_text(sk))))[:6]) for sk in skills]


async def rank(question: str, finding_kinds: set[str], eligible: list[Any], embed: Embed | None = None, signature: list[str] | None = None) -> tuple[list[dict], str]:
    query = symptom_text(question, finding_kinds, signature)
    method, scored = "tfidf", None
    if embed is not None:
        try:
            scored = await semantic(query, eligible, embed)
            method = "embedding" if scored is not None else "tfidf"
        except Exception as exc:
            log.warning("skill embedding unavailable", extra={"event": "skill_rank_embed_failed", "error_type": type(exc).__name__})
            ML_FALLBACKS.labels("skill_recommendations", "embedding_unavailable").inc()
    if scored is None:
        scored = lexical(query, eligible)
    out = [{"skill": sk.name, "version": sk.meta.version, "score": round(s, 4), "matched": terms,
            "why": (f"shares {', '.join(terms)} with the symptoms" if terms else "similar wording to the symptoms")} for sk, s, terms in scored]
    out.sort(key=lambda r: (-r["score"], r["skill"]))
    return out, method


def combine(mode: str, deterministic: list[tuple[Any, int, list[str]]], ranked: list[dict], by_name: dict[str, Any], limit: int) -> tuple[list[tuple[Any, int, list[str]]], list[str]]:
    """The deterministic choice first and unchanged; in enabled mode, eligible skills above the similarity floor
    fill the places left. Returns (skills to run, names added)."""
    if mode != "enabled":
        return deterministic, []
    chosen = {sk.name for sk, _, _ in deterministic}
    out, added = list(deterministic), []
    for r in ranked:
        if len(out) >= limit:
            break
        if r["skill"] in chosen or r["score"] < MIN_SIMILARITY or r["skill"] not in by_name:
            continue
        out.append((by_name[r["skill"]], 0, [f"semantic: {r['why']} (similarity {r['score']})", "recommended by semantic ranking; the rules did not pick it"]))
        added.append(r["skill"])
    return out, added
