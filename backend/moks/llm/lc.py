"""LangChain's chat model interface over MOKS providers.

Every call still goes through llm.budget, so reservations, daily limits, metering and redacted evidence
apply exactly as they do elsewhere. Vendor packages such as langchain-openai are deliberately not used:
they would talk to the provider directly, past all of that.

Importing this turns LangSmith tracing off, whatever the environment says. It would send prompts, which
hold redacted evidence, to a third party, and nothing in MOKS may do that by a variable being set.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from . import budget

for name in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"):
    os.environ[name] = "false"

ROLES = {"human": "user", "ai": "assistant"}


class MoksChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: Any
    max_tokens: int
    json_mode: bool = True
    user_id: uuid.UUID | None = None
    investigation_id: uuid.UUID | None = None
    purpose: str = "investigation"

    @property
    def _llm_type(self) -> str:
        return f"moks-{self.provider.name}"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("MOKS is async: use ainvoke")

    async def _agenerate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        system = "\n\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        turns = [{"role": ROLES[m.type], "content": str(m.content)} for m in messages if m.type in ROLES]
        resp = await budget.complete(self.provider, system, turns, json_mode=self.json_mode, max_tokens=self.max_tokens, user_id=self.user_id,
                                     investigation_id=self.investigation_id, purpose=self.purpose)
        message = AIMessage(content=resp.text, usage_metadata={"input_tokens": resp.tokens_in, "output_tokens": resp.tokens_out, "total_tokens": resp.tokens_in + resp.tokens_out},
                            response_metadata={"model": resp.model, "provider": resp.provider, "cost_usd": str(resp.cost_usd)})
        return ChatResult(generations=[ChatGeneration(message=message)])
