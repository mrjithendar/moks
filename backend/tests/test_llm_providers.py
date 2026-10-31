"""Every provider through its real request and response shape, with the network simulated."""
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from moks.llm import providers as p
from moks.llm.providers import LLMError, provider_for
from moks.models import LLMConfig


def cfg(provider, model="m", endpoint=None, name="MOKS_CRED_TEST_KEY"):
    return LLMConfig(id=uuid.uuid4(), name="x", purpose="chat", provider=provider, model=model, endpoint=endpoint, credential_ref={"type": "env", "name": name},
                     timeout_seconds=5, max_output_tokens=500, input_price_per_mtok=0, output_price_per_mtok=0)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("MOKS_CRED_TEST_KEY", "secret-value\n")
    monkeypatch.setattr(p, "_wait", lambda r, a: 0)


def transport(*responses):
    seen = []

    def handler(req):
        seen.append(req)
        r = responses[min(len(seen) - 1, len(responses) - 1)]
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)
    return httpx.MockTransport(handler), seen


CHAT = {"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}


async def ask(provider, t, json_mode=True):
    return await provider_for(provider, t).complete("sys", [{"role": "user", "content": "hi"}], json_mode=json_mode, max_tokens=100)


async def test_openai_shape():
    t, seen = transport(CHAT)
    r = await ask(cfg("openai"), t)
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions" and seen[0].headers["authorization"] == "Bearer secret-value"
    assert body["max_completion_tokens"] == 100 and body["response_format"] == {"type": "json_object"}
    assert (r.text, r.tokens_in, r.tokens_out) == ('{"ok": true}', 3, 2)


@pytest.mark.parametrize("endpoint", ["https://api.mistral.ai/v1", "https://api.groq.com/openai/v1", "https://openrouter.ai/api/v1", "https://api.deepseek.com/v1"])
async def test_compatible_servers_use_their_own_endpoint_and_max_tokens(endpoint):
    t, seen = transport(CHAT)
    await ask(cfg("openai", endpoint=endpoint), t)
    assert str(seen[0].url) == f"{endpoint}/chat/completions" and json.loads(seen[0].content)["max_tokens"] == 100


async def test_a_local_server_needs_no_key(monkeypatch):
    t, seen = transport(CHAT)
    c = cfg("openai", endpoint="http://vllm:8000/v1")
    c.credential_ref = {"type": "none"}
    await ask(c, t)
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize("error,removed", [("Unsupported value: 'temperature' does not support 0.1", "temperature"), ("response_format is not supported", "response_format")])
async def test_a_server_refusing_a_parameter_gets_the_request_without_it(error, removed):
    t, seen = transport(httpx.Response(400, json={"error": {"message": error}}), CHAT)
    r = await ask(cfg("openai", endpoint="https://x.example/v1"), t)
    assert r.text and removed not in json.loads(seen[1].content)


async def test_max_tokens_field_is_swapped_when_refused():
    t, seen = transport(httpx.Response(400, text="Unsupported parameter: 'max_tokens'"), CHAT)
    await ask(cfg("openai", endpoint="https://x.example/v1"), t)
    assert "max_completion_tokens" in json.loads(seen[1].content)


async def test_an_unrelated_400_is_not_retried():
    t, seen = transport(httpx.Response(400, text="model not found"))
    with pytest.raises(LLMError) as e:
        await ask(cfg("openai"), t)
    assert e.value.kind == "bad_response" and len(seen) == 1


@pytest.mark.parametrize("status", [429, 503, 529])
async def test_rate_limits_and_overload_are_waited_out(status):
    t, seen = transport(httpx.Response(status, headers={"retry-after": "1"}), CHAT)
    assert (await ask(cfg("openai"), t)).text and len(seen) == 2


async def test_rate_limit_that_persists_is_reported():
    t, seen = transport(httpx.Response(429))
    with pytest.raises(LLMError) as e:
        await ask(cfg("openai"), t)
    assert e.value.kind == "rate_limited" and len(seen) == p.RETRIES + 1


async def test_auth_errors_never_show_the_key():
    t, _ = transport(httpx.Response(401, text="bad key"))
    with pytest.raises(LLMError) as e:
        await ask(cfg("openai"), t)
    assert e.value.kind == "auth" and "secret-value" not in e.value.message


async def test_empty_answers_say_why():
    t, _ = transport({"choices": [{"message": {"content": None, "refusal": "I can't"}, "finish_reason": "stop"}]})
    with pytest.raises(LLMError, match="I can't"):
        await ask(cfg("openai"), t)


async def test_content_as_a_list_of_parts():
    t, _ = transport({"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}]})
    assert (await ask(cfg("openai", endpoint="https://x.example/v1"), t)).text == "ab"


async def test_azure_uses_the_v1_api_and_its_own_header():
    t, seen = transport(CHAT)
    await ask(cfg("azure_openai", "my-deployment", "https://res.openai.azure.com"), t)
    assert str(seen[0].url) == "https://res.openai.azure.com/openai/v1/chat/completions" and seen[0].headers["api-key"] == "secret-value"
    assert "authorization" not in seen[0].headers and json.loads(seen[0].content)["model"] == "my-deployment"


async def test_azure_needs_an_endpoint():
    t, _ = transport(CHAT)
    with pytest.raises(LLMError, match="resource endpoint"):
        await ask(cfg("azure_openai"), t)


async def test_gemini_shape():
    t, seen = transport({"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}, {"text": "shown"}]}}], "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2}})
    r = await ask(cfg("gemini", "gemini-x"), t)
    assert "models/gemini-x:generateContent" in str(seen[0].url) and seen[0].headers["x-goog-api-key"] == "secret-value" and r.text == "shown"


class Claude:
    """Stands in for the Anthropic SDK client, which is what talks to the API."""
    calls: list = []
    answer = None
    raises = None

    def __init__(self, **kw):
        Claude.init = kw
        self.messages = SimpleNamespace(create=self._create(False))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create(True)))

    def _create(self, beta):
        async def create(**kw):
            Claude.calls.append({"beta": beta, **kw})
            if Claude.raises:
                raise Claude.raises
            return Claude.answer
        return create

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


def message(text="hello", stop="end_turn"):
    blocks = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)] if text else []
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=SimpleNamespace(input_tokens=7, output_tokens=9))


@pytest.fixture
def claude(monkeypatch):
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Claude)
    Claude.calls, Claude.answer, Claude.raises = [], message(), None
    return Claude


async def test_claude_request_sends_no_sampling_or_prefill_and_reads_only_text(claude):
    r = await provider_for(cfg("anthropic", "claude-opus-5-5")).complete("sys", [{"role": "user", "content": "hi"}], json_mode=True, max_tokens=100)
    call = claude.calls[0]
    assert (r.text, r.tokens_in, r.tokens_out) == ("hello", 7, 9)
    assert "temperature" not in call and call["messages"][-1]["role"] == "user" and "JSON object" in call["system"]
    assert call["max_tokens"] == 4000  # thinking shares this limit
    assert claude.init["api_key"] == "secret-value"


async def test_claude_fallback_is_used_on_models_that_support_it_and_not_through_a_gateway(claude):
    await provider_for(cfg("anthropic", "claude-opus-5-5")).complete("s", [{"role": "user", "content": "h"}], json_mode=False, max_tokens=100)
    assert claude.calls[-1]["beta"] and claude.calls[-1]["fallbacks"] == "default"
    await provider_for(cfg("anthropic", "claude-haiku-5-5")).complete("s", [{"role": "user", "content": "h"}], json_mode=False, max_tokens=100)
    assert not claude.calls[-1]["beta"] and "fallbacks" not in claude.calls[-1]
    await provider_for(cfg("anthropic", "claude-opus-5-5", "https://gateway.example")).complete("s", [{"role": "user", "content": "h"}], json_mode=False, max_tokens=100)
    assert not claude.calls[-1]["beta"] and claude.init["base_url"] == "https://gateway.example"


async def test_claude_refusal_and_empty_answers_are_errors(claude):
    claude.answer = message("", "refusal")
    with pytest.raises(LLMError) as e:
        await provider_for(cfg("anthropic", "claude-opus-5-5")).complete("s", [{"role": "user", "content": "h"}], json_mode=False, max_tokens=100)
    assert e.value.kind == "refused"
    claude.answer = message("", "max_tokens")
    with pytest.raises(LLMError, match="max_tokens"):
        await provider_for(cfg("anthropic", "claude-opus-5-5")).complete("s", [{"role": "user", "content": "h"}], json_mode=False, max_tokens=100)


@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "auth"), (429, "rate_limited"), (400, "bad_response"), (529, "unavailable")])
async def test_claude_errors_map_to_the_shared_kinds(claude, status, kind):
    import anthropic

    resp = httpx.Response(status, request=httpx.Request("POST", "https://x"), json={"error": {"message": "m"}})
    cls = {401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError, 429: anthropic.RateLimitError, 400: anthropic.BadRequestError}.get(status, anthropic.InternalServerError)
    err = cls.__new__(cls)
    err.status_code, err.message, err.response = status, "m", resp
    claude.raises = err
    with pytest.raises(LLMError) as e:
        await provider_for(cfg("anthropic", "claude-haiku-5-5")).complete("s", [{"role": "user", "content": "h"}], json_mode=False, max_tokens=100)
    assert e.value.kind == kind


async def test_claude_has_no_embeddings_and_says_so():
    with pytest.raises(LLMError, match="no embedding API"):
        await provider_for(cfg("anthropic")).embed(["x"])


def test_an_unknown_provider_is_a_clear_error():
    with pytest.raises(LLMError, match="unknown provider"):
        provider_for(cfg("nope"))
