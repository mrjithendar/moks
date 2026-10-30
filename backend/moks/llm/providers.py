"""Provider-neutral LLM access.

The orchestrator only needs "complete this conversation, preferably as JSON", which every
supported provider offers. Tool use is negotiated in our own JSON protocol (see
moks.orchestrator.prompts) rather than each vendor's function-calling API, so the same
policy-checked tool loop works with OpenAI, Gemini, Bedrock and local Ollama models.

The LLM never receives infrastructure credentials: it sees redacted evidence and returns
tool *requests*, which the executor validates and authorizes.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from ..connectors.base import ConnectorError, resolve_credential
from ..models import LLMConfig
from ..observability import LLM_COST, LLM_LATENCY, LLM_REQUESTS, LLM_TOKENS, tracer


class LLMError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # config | auth | unavailable | timeout | budget | bad_response
        self.message = message


@dataclass
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    provider: str
    model: str
    cost_usd: Decimal = Decimal("0")


Message = dict[str, str]  # {"role": "user"|"assistant", "content": str}


class Provider:
    name = "base"

    def __init__(self, cfg: LLMConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.transport = transport

    def _key(self, required: bool = True) -> str | None:
        try:
            key = resolve_credential(self.cfg.credential_ref)
        except ConnectorError as exc:
            raise LLMError("config", exc.message) from None
        if required and not key:
            raise LLMError("config", f"{self.name} needs an API key credential reference")
        return key

    async def _post(self, url: str, body: dict, headers: dict[str, str]) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds, transport=self.transport) as c:
                r = await c.post(url, json=body, headers=headers)
        except httpx.TimeoutException:
            raise LLMError("timeout", f"{self.name} did not answer within {self.cfg.timeout_seconds}s") from None
        except httpx.TransportError as exc:
            raise LLMError("unavailable", f"cannot reach {self.name} ({type(exc).__name__})") from None
        if r.status_code in (401, 403):
            raise LLMError("auth", f"{self.name} rejected the credentials (HTTP {r.status_code})")
        if r.status_code == 429:
            raise LLMError("unavailable", f"{self.name} rate limited the request (HTTP 429)")
        if r.status_code >= 400:
            raise LLMError("unavailable" if r.status_code >= 500 else "bad_response",
                           f"{self.name} returned HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            raise LLMError("bad_response", f"{self.name} returned non-JSON") from None

    async def complete(self, system: str, messages: list[Message], *, json_mode: bool, max_tokens: int,
                       temperature: float = 0.1) -> LLMResponse:
        raise NotImplementedError

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise LLMError("config", f"embeddings are not implemented for {self.name} yet (Phase 2)")


class OpenAIProvider(Provider):
    name = "openai"

    async def complete(self, system, messages, *, json_mode, max_tokens, temperature=0.1):
        base = (self.cfg.endpoint or "https://api.openai.com/v1").rstrip("/")
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": temperature,
        }
        # api.openai.com uses max_completion_tokens; most OpenAI-compatible servers use max_tokens.
        body["max_completion_tokens" if "api.openai.com" in base else "max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = await self._post(f"{base}/chat/completions", body, {"Authorization": f"Bearer {self._key()}"})
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError("bad_response", "openai response has no message content") from None
        usage = data.get("usage") or {}
        return LLMResponse(text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), self.name, self.cfg.model)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        base = (self.cfg.endpoint or "https://api.openai.com/v1").rstrip("/")
        body: dict[str, Any] = {"model": self.cfg.model, "input": texts}
        if self.cfg.embedding_dimensions:
            body["dimensions"] = self.cfg.embedding_dimensions
        data = await self._post(f"{base}/embeddings", body, {"Authorization": f"Bearer {self._key()}"})
        return [d["embedding"] for d in sorted(data.get("data", []), key=lambda d: d.get("index", 0))]


class GeminiProvider(Provider):
    name = "gemini"

    async def complete(self, system, messages, *, json_mode, max_tokens, temperature=0.1):
        base = (self.cfg.endpoint or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]} for m in messages],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        data = await self._post(f"{base}/models/{self.cfg.model}:generateContent", body, {"x-goog-api-key": self._key()})
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, TypeError):
            reason = (data.get("promptFeedback") or {}).get("blockReason") or (data.get("candidates") or [{}])[0].get("finishReason")
            raise LLMError("bad_response", f"gemini returned no content ({reason or 'unknown reason'})") from None
        usage = data.get("usageMetadata") or {}
        return LLMResponse(text, int(usage.get("promptTokenCount", 0)), int(usage.get("candidatesTokenCount", 0)), self.name, self.cfg.model)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        base = (self.cfg.endpoint or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        model = self.cfg.model if self.cfg.model.startswith("models/") else f"models/{self.cfg.model}"
        reqs = []
        for t in texts:
            r: dict[str, Any] = {"model": model, "content": {"parts": [{"text": t}]}}
            if self.cfg.embedding_dimensions:
                r["outputDimensionality"] = self.cfg.embedding_dimensions
            reqs.append(r)
        data = await self._post(f"{base}/{model}:batchEmbedContents", {"requests": reqs}, {"x-goog-api-key": self._key()})
        return [e["values"] for e in data.get("embeddings", [])]


class OllamaProvider(Provider):
    name = "ollama"

    async def complete(self, system, messages, *, json_mode, max_tokens, temperature=0.1):
        if not self.cfg.endpoint:
            raise LLMError("config", "ollama needs an endpoint, e.g. http://ollama.ollama:11434")
        body: dict[str, Any] = {
            "model": self.cfg.model, "stream": False,
            "messages": [{"role": "system", "content": system}] + messages,
            "options": {"num_predict": max_tokens, "temperature": temperature, "num_ctx": 16384},
        }
        if json_mode:
            body["format"] = "json"
        key = self._key(required=False)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        data = await self._post(f"{self.cfg.endpoint.rstrip('/')}/api/chat", body, headers)
        text = (data.get("message") or {}).get("content") or ""
        return LLMResponse(text, int(data.get("prompt_eval_count", 0)), int(data.get("eval_count", 0)), self.name, self.cfg.model)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.cfg.endpoint:
            raise LLMError("config", "ollama needs an endpoint")
        key = self._key(required=False)
        data = await self._post(f"{self.cfg.endpoint.rstrip('/')}/api/embed", {"model": self.cfg.model, "input": texts},
                                {"Authorization": f"Bearer {key}"} if key else {})
        return data.get("embeddings", [])


class BedrockProvider(Provider):
    """Amazon Bedrock Converse API. Credentials come from the AWS default chain: IRSA / EKS Pod
    Identity on EKS, or a mounted profile/short-lived keys on Minikube. Nothing is stored in MOKS."""

    name = "bedrock"

    async def complete(self, system, messages, *, json_mode, max_tokens, temperature=0.1):
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

        region = self.cfg.region or "us-east-1"
        prompt_system = system + ("\nRespond with a single JSON object and nothing else." if json_mode else "")

        def call():
            client = boto3.client("bedrock-runtime", region_name=region, endpoint_url=self.cfg.endpoint or None,
                                  config=Config(read_timeout=self.cfg.timeout_seconds, connect_timeout=5, retries={"max_attempts": 2}))
            return client.converse(
                modelId=self.cfg.model, system=[{"text": prompt_system}],
                messages=[{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )

        try:
            data = await asyncio.wait_for(asyncio.to_thread(call), self.cfg.timeout_seconds + 5)
        except asyncio.TimeoutError:
            raise LLMError("timeout", f"bedrock did not answer within {self.cfg.timeout_seconds}s") from None
        except NoCredentialsError:
            raise LLMError("auth", "no AWS credentials available to MOKS (configure IRSA/Pod Identity or a mounted profile)") from None
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            kind = "auth" if code in ("AccessDeniedException", "UnrecognizedClientException") else "unavailable"
            raise LLMError(kind, f"bedrock {code}: {exc.response.get('Error', {}).get('Message', '')[:200]}") from None
        except BotoCoreError as exc:
            raise LLMError("unavailable", f"bedrock: {type(exc).__name__}") from None
        content = (data.get("output") or {}).get("message", {}).get("content") or []
        text = "".join(c.get("text", "") for c in content)
        usage = data.get("usage") or {}
        return LLMResponse(text, int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0)), self.name, self.cfg.model)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Amazon Titan Text Embeddings v2 (or compatible): one InvokeModel call per text."""
        import json as _json

        import boto3

        region = self.cfg.region or "us-east-1"

        def call(t: str) -> list[float]:
            client = boto3.client("bedrock-runtime", region_name=region, endpoint_url=self.cfg.endpoint or None)
            body: dict[str, Any] = {"inputText": t[:30000]}
            if self.cfg.embedding_dimensions:
                body["dimensions"] = self.cfg.embedding_dimensions
            resp = client.invoke_model(modelId=self.cfg.model, body=_json.dumps(body), contentType="application/json", accept="application/json")
            return _json.loads(resp["body"].read())["embedding"]

        try:
            return [await asyncio.wait_for(asyncio.to_thread(call, t), self.cfg.timeout_seconds) for t in texts]
        except Exception as exc:
            raise LLMError("unavailable", f"bedrock embedding failed ({type(exc).__name__})") from None


PROVIDERS: dict[str, type[Provider]] = {
    "openai": OpenAIProvider, "gemini": GeminiProvider, "ollama": OllamaProvider, "bedrock": BedrockProvider,
}


def provider_for(cfg: LLMConfig, transport: httpx.AsyncBaseTransport | None = None) -> Provider:
    cls = PROVIDERS.get(cfg.provider)
    if not cls:
        raise LLMError("config", f"unknown provider {cfg.provider!r}")
    return cls(cfg, transport)


def estimate_cost(cfg: LLMConfig, tokens_in: int, tokens_out: int) -> Decimal:
    return (Decimal(tokens_in) * Decimal(cfg.input_price_per_mtok) + Decimal(tokens_out) * Decimal(cfg.output_price_per_mtok)) / Decimal(1_000_000)


async def complete_metered(provider: Provider, system: str, messages: list[Message], *, json_mode: bool, max_tokens: int) -> LLMResponse:
    cfg = provider.cfg
    start = time.monotonic()
    with tracer().start_as_current_span("llm.complete") as span:
        span.set_attribute("gen_ai.system", cfg.provider)
        span.set_attribute("gen_ai.request.model", cfg.model)
        try:
            resp = await provider.complete(system, messages, json_mode=json_mode, max_tokens=max_tokens)
        except LLMError as exc:
            LLM_REQUESTS.labels(cfg.provider, cfg.model, exc.kind).inc()
            LLM_LATENCY.labels(cfg.provider).observe(time.monotonic() - start)
            span.set_attribute("moks.outcome", exc.kind)
            raise
        span.set_attribute("gen_ai.usage.input_tokens", resp.tokens_in)
        span.set_attribute("gen_ai.usage.output_tokens", resp.tokens_out)
    resp.cost_usd = estimate_cost(cfg, resp.tokens_in, resp.tokens_out)
    LLM_REQUESTS.labels(cfg.provider, cfg.model, "ok").inc()
    LLM_LATENCY.labels(cfg.provider).observe(time.monotonic() - start)
    LLM_TOKENS.labels(cfg.provider, cfg.model, "input").inc(resp.tokens_in)
    LLM_TOKENS.labels(cfg.provider, cfg.model, "output").inc(resp.tokens_out)
    LLM_COST.labels(cfg.provider, cfg.model).inc(float(resp.cost_usd))
    return resp
