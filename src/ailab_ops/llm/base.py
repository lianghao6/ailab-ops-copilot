"""Provider-neutral types for chat + tool calling.

Shaped after the OpenAI chat-completions schema because that is the de-facto
interchange format: vLLM, SGLang, TGI, lmdeploy, one-api and most enterprise
gateways all speak some dialect of it. Keeping to that shape means the swap to
a real backend is configuration, not code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol, Sequence

Role = Literal["system", "user", "assistant", "tool"]


class FinishReason(str, Enum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    ERROR = "error"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments or json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass
class ChatMessage:
    role: Role
    content: str = ""
    name: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None

    def to_openai(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            msg["name"] = self.name
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        return msg

    def text_for_prompt(self) -> str:
        """Flattened form, used when a backend has no native tool protocol."""
        if self.role == "tool":
            return f"TOOL RESULT [{self.name or 'tool'}]: {self.content}"
        if self.tool_calls:
            calls = ", ".join(f"{tc.name}({json.dumps(tc.arguments, ensure_ascii=False)})" for tc in self.tool_calls)
            return f"{self.content}\nCALL: {calls}".strip()
        return self.content


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @staticmethod
    def from_openai(raw: dict[str, Any]) -> "ToolSpec":
        fn = raw.get("function", raw)
        return ToolSpec(
            name=fn["name"],
            description=fn.get("description", ""),
            parameters=fn.get("parameters", {"type": "object", "properties": {}}),
        )


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.tokens_in + other.tokens_in, self.tokens_out + other.tokens_out)


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: FinishReason = FinishReason.STOP
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    latency_s: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    """What the agent loop requires of a model backend."""

    model: str

    def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ): ...


def approx_tokens(text: str) -> int:
    """Cheap token estimate.

    ~4 characters per token holds well for English prose and acceptably for
    code; it is deliberately not a real tokeniser, because the point of the
    accounting is relative cost attribution and rate limiting, both of which
    are insensitive to a small constant factor. A production system would use
    the served model's own tokeniser.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def messages_to_text(messages: Sequence[ChatMessage]) -> str:
    return "\n".join(m.text_for_prompt() for m in messages)
