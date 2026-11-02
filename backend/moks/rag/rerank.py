"""Client for the internal reranking service. Optional, off by default, and never a reason for a search
to fail: anything other than a clean, matching answer leaves the existing order in place.

The service only ever sees passages search has already authorized, and it returns ids and scores. The
text and the citations stay here. A reranker score orders one result list. It is not a probability,
and it does not show that a passage supports an answer, so nothing here feeds verification, skill
selection, severity or any policy decision.
"""
from __future__ import annotations

import math
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import get_settings
from ..observability import RERANK_CALLS, RERANK_CANDIDATES, RERANK_SECONDS, RERANK_TRUNCATED

# Ceilings that hold whatever an administrator configures.
MAX_CANDIDATES = 50
MAX_RESULTS = 50
MAX_TIMEOUT_MS = 5000
MAX_QUERY_CHARS = 2000
MAX_PASSAGE_CHARS = 8000
FAILURES_TO_OPEN = 5
OPEN_SECONDS = 30.0
NOTE = "Reranker scores order this result list. They are not probabilities and do not show that a passage supports an answer."


@dataclass
class Outcome:
    scores: dict[str, float] | None = None  # None: keep the existing order
    reason: str | None = None
    model: str | None = None
    revision: str | None = None
    candidates: int = 0
    truncated: list[str] = field(default_factory=list)  # did not fit the model's input, so only part was scored
    clipped: list[str] = field(default_factory=list)    # cut here before sending, for the same reason
    inference_ms: float | None = None
    elapsed_ms: float | None = None


class Reranker:
    def __init__(self, url: str, token: str, model: str, revision: str, candidates: int = 30, timeout_ms: int = 1500, max_query_chars: int = 1000,
                 max_passage_chars: int = 2000, transport: httpx.AsyncBaseTransport | None = None):
        self.url, self.token, self.model, self.revision = url.rstrip("/"), token, model, revision
        self.candidates = max(1, min(candidates, MAX_CANDIDATES))
        self.timeout_ms = max(10, min(timeout_ms, MAX_TIMEOUT_MS))
        self.max_query_chars = max(1, min(max_query_chars, MAX_QUERY_CHARS))
        self.max_passage_chars = max(1, min(max_passage_chars, MAX_PASSAGE_CHARS))
        self._transport = transport
        self._failures = 0
        self._open_until = 0.0
        self._clock = time.monotonic
        self.counts: dict[str, int] = {}
        self.elapsed_total_ms = 0.0
        self.candidates_total = 0

    def _count(self, outcome: str) -> None:
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        RERANK_CALLS.labels(outcome).inc()

    def _failed(self, reason: str, seconds: float = OPEN_SECONDS) -> Outcome:
        self._failures += 1
        if self._failures >= FAILURES_TO_OPEN:
            self._open_until = self._clock() + seconds
        self._count(reason)
        return Outcome(reason=reason)

    def _parse(self, body: Any, sent: list[str]) -> dict[str, float] | str:
        """Anything unexpected is refused whole: a partial ranking cannot be told from a wrong one."""
        if not isinstance(body, dict) or not isinstance(body.get("results"), list) or not isinstance(body.get("model"), dict):
            return "invalid_output"
        if body["model"].get("id") != self.model or body["model"].get("revision") != self.revision:
            return "model_mismatch"
        scores: dict[str, float] = {}
        for row in body["results"]:
            if not isinstance(row, dict) or set(row) != {"id", "score"}:
                return "invalid_output"
            cid, score = row["id"], row["score"]
            if cid in scores or cid not in sent or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                return "invalid_output"
            scores[cid] = float(score)
        return scores if set(scores) == set(sent) else "invalid_output"

    async def rerank(self, query: str, items: list[tuple[str, str]]) -> Outcome:
        """`items` must already be authorized. Never raises for a service failure."""
        if not items:
            return Outcome(reason="no_candidates")
        if self._clock() < self._open_until:
            self._count("circuit_open")
            return Outcome(reason="circuit_open")
        items = items[:self.candidates]
        sent = [cid for cid, _ in items]
        clipped = [cid for cid, text in items if len(text) > self.max_passage_chars]
        body = {"request_id": uuid.uuid4().hex, "query": query[:self.max_query_chars], "limit": len(items), "timeout_ms": self.timeout_ms,
                "candidates": [{"id": cid, "text": text[:self.max_passage_chars]} for cid, text in items]}
        start = self._clock()
        RERANK_CANDIDATES.observe(len(items))
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=(self.timeout_ms + 100) / 1000) as client:
                r = await client.post(f"{self.url}/rerank", json=body, headers={"Authorization": f"Bearer {self.token}"})
        except httpx.TimeoutException:
            return self._failed("timeout")
        except httpx.HTTPError:
            return self._failed("unavailable")
        if r.status_code == 429:
            return self._failed("overload", _retry_after(r))
        if r.status_code == 504:
            return self._failed("timeout")
        if r.status_code in (401, 403):
            return self._failed("auth")
        if r.status_code != 200:
            return self._failed("unavailable")
        try:
            parsed = self._parse(r.json(), sent)
        except ValueError:
            parsed = "invalid_output"
        if isinstance(parsed, str):
            return self._failed(parsed)
        self._failures, self._open_until = 0, 0.0
        elapsed = (self._clock() - start) * 1000
        info = r.json()
        truncated = [c for c in (info.get("truncation") or {}).get("truncated_ids", []) if c in parsed]
        self._count("applied")
        self.elapsed_total_ms += elapsed
        self.candidates_total += len(items)
        RERANK_SECONDS.observe(elapsed / 1000)
        RERANK_TRUNCATED.inc(len(set(truncated) | set(clipped)))
        return Outcome(parsed, None, self.model, self.revision, len(items), truncated, clipped, info.get("duration_ms"), round(elapsed, 1))

    async def ready(self) -> dict[str, Any]:
        """For the administration page only. Reports what the service says, or that it could not be asked."""
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=2.0) as client:
                r = await client.get(f"{self.url}/readyz")
            body = r.json()
        except (httpx.HTTPError, ValueError):
            return {"ready": False, "reachable": False, "model": None}
        return {"ready": r.status_code == 200 and body.get("ready") is True, "reachable": True, "model": body.get("model")}

    def stats(self) -> dict[str, Any]:
        applied = self.counts.get("applied", 0)
        total = sum(self.counts.values())
        return {"calls": total, "applied": applied, "fallbacks": {k: v for k, v in self.counts.items() if k != "applied"}, "average_ms": round(self.elapsed_total_ms / applied, 1) if applied else None,
                "average_candidates": round(self.candidates_total / applied, 1) if applied else None, "scope": "this process, since it started", "circuit_open": self._clock() < self._open_until}


_client: Reranker | None = None
_key: tuple | None = None


def active() -> Reranker | None:
    """The configured client, or None unless it is enabled and pins a model and revision."""
    global _client, _key
    s = get_settings()
    if not (s.rag_rerank_enabled and s.rag_rerank_url and s.rag_rerank_model and s.rag_rerank_revision):
        return None
    key = (s.rag_rerank_url, s.rag_rerank_model, s.rag_rerank_revision, s.rag_rerank_candidates, s.rag_rerank_timeout_ms, s.rag_rerank_max_query_chars, s.rag_rerank_max_passage_chars)
    if key != _key:
        _key = key
        _client = Reranker(s.rag_rerank_url, os.environ.get("MOKS_CRED_RERANKER_TOKEN", ""), s.rag_rerank_model, s.rag_rerank_revision, s.rag_rerank_candidates, s.rag_rerank_timeout_ms,
                           s.rag_rerank_max_query_chars, s.rag_rerank_max_passage_chars)
    return _client


def _retry_after(r: httpx.Response) -> float:
    try:
        return max(1.0, min(float(r.headers.get("retry-after", OPEN_SECONDS)), OPEN_SECONDS))
    except ValueError:
        return OPEN_SECONDS
