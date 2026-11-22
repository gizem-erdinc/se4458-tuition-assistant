"""LLM adapters with native tool/function calling.

The orchestrator keeps the conversation in a provider-neutral format and each
adapter translates it to the provider's wire format:

    {"role": "user",      "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...]}
    {"role": "tool",      "tool_call_id": str, "name": str, "content": str}

Providers
* ``anthropic`` — Claude Messages API (``tool_use`` / ``tool_result`` blocks)
* ``openai``    — any OpenAI-compatible Chat Completions endpoint (OpenAI,
  Groq, Ollama, OpenRouter, Gemini's OpenAI-compatible endpoint, ...), selected
  with ``LLM_BASE_URL``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]   # JSON Schema


class LLMClient(Protocol):
    model: str

    async def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, model: str, api_key: str | None = None, max_tokens: int = 1024):
        from anthropic import AsyncAnthropic
        self.client = AsyncAnthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.model, self.max_tokens = model, max_tokens

    @staticmethod
    def _convert(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
                if not blocks:
                    blocks.append({"type": "text", "text": "(empty)"})
                out.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                # consecutive tool results must be sent in ONE user turn
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    async def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> LLMResponse:
        resp = await self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=self._convert(messages),
            tools=[{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        return LLMResponse(text, calls, {"input_tokens": resp.usage.input_tokens,
                                         "output_tokens": resp.usage.output_tokens}, resp.model)


class OpenAICompatibleLLM:
    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY") or "not-needed",
                                  base_url=base_url or os.environ.get("LLM_BASE_URL") or None)
        self.model = model

    @staticmethod
    def _convert(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": tc.id, "type": "function",
                                          "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                                         for tc in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    async def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> LLMResponse:
        from openai import BadRequestError
        request = dict(model=self.model, messages=self._convert(system, messages), temperature=0,
                       tools=[{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                "parameters": t.parameters}} for t in tools])
        try:
            resp = await self.client.chat.completions.create(**request)
        except BadRequestError as e:
            # Some providers (Groq) reject a turn whose generated tool call is malformed
            # ("tool_use_failed" / "tool call validation failed"). Sampling again usually fixes it.
            if "tool" not in str(e).lower():
                raise
            resp = await self.client.chat.completions.create(**{**request, "temperature": 0.3})
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": tc.function.arguments}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        usage = {"input_tokens": resp.usage.prompt_tokens, "output_tokens": resp.usage.completion_tokens} \
            if resp.usage else {}
        return LLMResponse(msg.content or "", calls, usage, resp.model)


def llm_from_env() -> LLMClient:
    provider = os.environ.get("LLM_PROVIDER", "anthropic").lower()
    if provider == "anthropic":
        return AnthropicLLM(os.environ.get("LLM_MODEL", "claude-haiku-5-5"))
    if provider in {"openai", "openai-compatible", "groq", "ollama"}:
        return OpenAICompatibleLLM(os.environ.get("LLM_MODEL", "gpt-4.1-mini"))
    raise ValueError(f"Unknown LLM_PROVIDER '{provider}' (use 'anthropic' or 'openai')")
