"""The wire format, strict on both sides. Nothing in a request can name a model, a path or a URL."""
from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, field_validator, model_validator

# Hard ceilings. Deployment settings may lower them, never raise them.
MAX_CANDIDATES = 50
MAX_QUERY_CHARS = 2000
MAX_PASSAGE_CHARS = 8000
MAX_ID_CHARS = 64


def limit(name: str, ceiling: int) -> int:
    return min(int(os.environ.get(name, ceiling)), ceiling)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1, max_length=MAX_ID_CHARS, pattern=r"^[A-Za-z0-9_-]+$")
    text: StrictStr = Field(min_length=1)


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    query: StrictStr = Field(min_length=1)
    candidates: list[Candidate] = Field(min_length=1)
    limit: StrictInt = Field(ge=1)
    timeout_ms: StrictInt = Field(default=2000, ge=10, le=30_000)

    @model_validator(mode="after")
    def _bounded(self) -> "RerankRequest":
        most = limit("RERANKER_MAX_CANDIDATES", MAX_CANDIDATES)
        if len(self.candidates) > most:
            raise ValueError(f"at most {most} candidates")
        if len(self.query) > limit("RERANKER_MAX_QUERY_CHARS", MAX_QUERY_CHARS):
            raise ValueError("query too long")
        longest = limit("RERANKER_MAX_PASSAGE_CHARS", MAX_PASSAGE_CHARS)
        if any(len(c.text) > longest for c in self.candidates):
            raise ValueError("passage too long")
        ids = [c.id for c in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate candidate ids")
        return self


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr
    score: StrictFloat

    @field_validator("score")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("score is not finite")
        return v


class Truncation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_length_tokens: int
    query_tokens: int
    truncated_ids: list[str]


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    revision: str


class RerankResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    results: list[Result]
    model: ModelInfo
    duration_ms: float
    queue_ms: float
    truncation: Truncation
