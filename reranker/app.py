"""Internal text reranking service. It orders passages it is given and returns ids and scores. It keeps
nothing, calls nothing, and has no public route. Queries and passages are never logged."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from engine import Expired, Gate, Model, ModelError, Overloaded
from schemas import ModelInfo, RerankRequest, RerankResponse, Result, Truncation

log = logging.getLogger("reranker")
BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.4)
REQUESTS = Counter("reranker_requests_total", "Rerank requests", ["outcome"])
QUEUE_WAIT = Histogram("reranker_queue_wait_seconds", "Time spent waiting for a worker", buckets=BUCKETS)
INFERENCE = Histogram("reranker_inference_seconds", "Model inference time", buckets=BUCKETS)
CANDIDATES = Histogram("reranker_candidates", "Candidates per request", buckets=(1, 5, 10, 20, 30, 40, 50))
TRUNCATED = Counter("reranker_truncated_passages_total", "Passages that did not fit the model's input")
LOAD_FAILURES = Counter("reranker_model_load_failures_total", "Failed model loads")
HELD = Gauge("reranker_slots_held", "Slots held by running or queued work")
RETRY_AFTER_SECONDS = "1"


def _setting(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def create_app(model: Any = None, gate: Gate | None = None) -> FastAPI:
    token = os.environ.get("RERANKER_TOKEN", "")
    if not token:
        raise RuntimeError("RERANKER_TOKEN is required: this service has no unauthenticated mode")
    state: dict[str, Any] = {"ready": False, "draining": False, "error": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal model, gate
        threads = _setting("RERANKER_THREADS", 2)
        model = model or Model(os.environ.get("RERANKER_MODEL_DIR", "/opt/model"), threads, _setting("RERANKER_BATCH_SIZE", 16))
        gate = gate or Gate(_setting("RERANKER_CONCURRENCY", 1), _setting("RERANKER_QUEUE", 4))
        start = time.monotonic()
        try:
            await asyncio.to_thread(model.load)
            await asyncio.to_thread(model.warm_up)  # the first real request must not pay for lazy initialization
            state["ready"] = True
            log.info(json.dumps({"event": "model_ready", "model": model.id, "revision": model.revision, "seconds": round(time.monotonic() - start, 2)}))
        except Exception as exc:
            LOAD_FAILURES.inc()
            state["error"] = type(exc).__name__  # stays up and not ready, so the failure can be read from a probe
            log.error(json.dumps({"event": "model_load_failed", "errorType": type(exc).__name__}))
        yield
        state["ready"], state["draining"] = False, True
        await asyncio.to_thread(gate.close)  # work already accepted finishes; everything else was refused

    app = FastAPI(title="MOKS reranker", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default echoes the input, and the input is sensitive.
        return JSONResponse({"detail": "invalid request", "errors": [{"loc": [str(x) for x in e["loc"]], "type": e["type"]} for e in exc.errors()[:10]]}, status_code=422)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ready = state["ready"] and not state["draining"]
        body = {"ready": ready, "model": {"id": model.id, "revision": model.revision} if model else None, "error": state["error"]}
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.post("/rerank", response_model=RerankResponse)
    async def rerank(body: RerankRequest, authorization: str = Header(default="")) -> RerankResponse:
        if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            REQUESTS.labels("unauthorized").inc()
            raise HTTPException(401, "unauthorized")
        if not state["ready"]:
            REQUESTS.labels("not_ready").inc()
            raise HTTPException(503, "model not ready", headers={"Retry-After": RETRY_AFTER_SECONDS})
        deadline = time.monotonic() + body.timeout_ms / 1000
        candidates = [(c.id, c.text) for c in body.candidates]
        CANDIDATES.observe(len(candidates))

        def work(waited: float) -> Any:
            QUEUE_WAIT.observe(waited)
            if time.monotonic() > deadline:
                raise Expired()
            start = time.monotonic()
            out = model.score(body.query, candidates, deadline)
            return out, waited, time.monotonic() - start

        try:
            future = gate.submit(work)
        except Overloaded:
            REQUESTS.labels("overloaded").inc()
            raise HTTPException(429, "at capacity", headers={"Retry-After": RETRY_AFTER_SECONDS}) from None
        HELD.set(gate.held)
        try:
            # shield: a cancelled handler stops waiting and the work keeps its slot until it is really over
            scored, waited, took = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), max(deadline - time.monotonic(), 0))
        except (TimeoutError, asyncio.TimeoutError, Expired):
            REQUESTS.labels("timeout").inc()
            raise HTTPException(504, "deadline exceeded") from None
        except Exception as exc:
            REQUESTS.labels("error").inc()
            log.error(json.dumps({"event": "inference_failed", "request_id": body.request_id, "errorType": type(exc).__name__}))
            raise HTTPException(500, "inference failed") from None
        finally:
            HELD.set(gate.held)
        if len(scored.scores) != len(candidates) or any(s != s or s in (float("inf"), float("-inf")) for s in scored.scores):
            REQUESTS.labels("invalid_output").inc()
            log.error(json.dumps({"event": "invalid_model_output", "request_id": body.request_id}))
            raise HTTPException(500, "invalid model output")
        INFERENCE.observe(took)
        TRUNCATED.inc(len(scored.truncated))
        REQUESTS.labels("ok").inc()
        ranked = sorted(zip(candidates, scored.scores), key=lambda p: (-p[1], p[0][0]))[:body.limit]
        log.info(json.dumps({"event": "reranked", "request_id": body.request_id, "candidates": len(candidates), "truncated": len(scored.truncated), "duration_ms": round(took * 1000, 1), "queue_ms": round(waited * 1000, 1)}))
        return RerankResponse(request_id=body.request_id, results=[Result(id=c[0], score=s) for c, s in ranked], model=ModelInfo(id=model.id, revision=model.revision),
                              duration_ms=round(took * 1000, 2), queue_ms=round(waited * 1000, 2),
                              truncation=Truncation(max_length_tokens=scored.max_length, query_tokens=scored.query_tokens, truncated_ids=scored.truncated))

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    port = _setting("RERANKER_METRICS_PORT", 9464)
    if port:
        start_http_server(port)
    uvicorn.run(create_app(), host="0.0.0.0", port=_setting("RERANKER_PORT", 8090), access_log=False, timeout_graceful_shutdown=_setting("RERANKER_DRAIN_SECONDS", 20))  # noqa: S104


if __name__ == "__main__":
    main()
