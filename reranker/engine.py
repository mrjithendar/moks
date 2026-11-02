"""The model and the capacity around it. No web framework here, so both can be tested without one."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("reranker")


class Overloaded(Exception):
    """Every slot and queue place is taken."""


class Expired(Exception):
    """The deadline passed before the work could finish."""


class ModelError(Exception):
    """The model produced something unusable."""


@dataclasses.dataclass(frozen=True)
class Scored:
    scores: list[float]
    truncated: list[str]       # ids whose pair did not fit max_length, so only part of the text was scored
    query_tokens: int
    max_length: int


class Gate:
    """Bounded work on a fixed set of threads. A slot belongs to the computation, not to whoever asked:
    it is released by the future finishing, so a caller that timed out or disconnected frees nothing
    while the CPU is still busy. Work still in the queue when its deadline passes never starts."""

    def __init__(self, workers: int, queue: int):
        self.capacity = workers + queue
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rerank")
        self._lock = threading.Lock()
        self._held = 0
        self._closed = False

    @property
    def held(self) -> int:
        with self._lock:
            return self._held

    def submit(self, fn: Callable[[float], Any]) -> concurrent.futures.Future:
        """`fn` receives how long the work waited in the queue."""
        with self._lock:
            if self._closed or self._held >= self.capacity:
                raise Overloaded()
            self._held += 1
        queued_at = time.monotonic()
        try:
            future = self._pool.submit(lambda: fn(time.monotonic() - queued_at))
        except RuntimeError:
            self._release()
            raise Overloaded() from None
        future.add_done_callback(lambda _: self._release())
        return future

    def _release(self) -> None:
        with self._lock:
            self._held -= 1

    def close(self) -> None:
        """Refuses new work and waits for what was accepted."""
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=True)


class Model:
    """One loaded cross-encoder. Weights come from local files only, and remote code is never allowed."""

    def __init__(self, path: str, threads: int, batch_size: int):
        self.path = Path(path)
        self.batch_size = batch_size
        self.lock = json.loads((self.path / "model.lock.json").read_text()) if (self.path / "model.lock.json").exists() else {}
        self.id, self.revision = self.lock.get("model_id", ""), self.lock.get("revision", "")
        self.max_length = int(self.lock.get("max_length", 512))
        self._threads = threads
        self._encoder: Any = None
        self._tokenizer: Any = None

    def load(self) -> None:
        import torch
        from sentence_transformers import CrossEncoder

        if not self.id or not self.revision:
            raise ModelError("the model directory has no model.lock.json")
        torch.set_num_threads(self._threads)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:  # already fixed in this process
            pass
        self._identity = torch.nn.Identity()
        self._encoder = CrossEncoder(str(self.path), device="cpu", max_length=self.max_length, local_files_only=True, trust_remote_code=False,
                                     activation_fn=self._identity, model_kwargs={"use_safetensors": True})
        self._encoder.eval()
        self._tokenizer = self._encoder.tokenizer

    def warm_up(self) -> None:
        self.score("warm up query", [("warm", "warm up passage " * 40)], deadline=time.monotonic() + 600)

    def _tokens(self, text: str) -> int:
        return len(self._tokenizer(text, add_special_tokens=False, truncation=False, verbose=False)["input_ids"])

    def score(self, query: str, candidates: list[tuple[str, str]], deadline: float) -> Scored:
        """Scores in batches and checks the deadline between them, because a running batch cannot be
        stopped. Scores are raw logits: a relative order within this call, not a probability."""
        query_tokens = self._tokens(query)
        room = self.max_length - query_tokens - self._tokenizer.num_special_tokens_to_add(pair=True)
        truncated = [cid for cid, text in candidates if self._tokens(text) > room]
        scores: list[float] = []
        for start in range(0, len(candidates), self.batch_size):
            if time.monotonic() > deadline:
                raise Expired()
            batch = candidates[start:start + self.batch_size]
            out = self._encoder.predict([(query, text) for _, text in batch], batch_size=self.batch_size, show_progress_bar=False, activation_fn=self._identity, convert_to_numpy=True)
            scores += [float(s) for s in out]
        return Scored(scores, truncated, query_tokens, self.max_length)
