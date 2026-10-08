"""Exact-input replay: no synthesis, fuzzy matching, or sequential fallback."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from ailab_ops.llm.base import ChatMessage, FinishReason, LLMResponse, ToolCall, ToolSpec, Usage
from .protocol import ModelBackendError, ModelConfigurationError


class ReplayMissError(ModelBackendError):
    def __init__(self, request_hash: str):
        super().__init__(f"No recorded model response for request {request_hash}", kind="replay_miss")
        self.request_hash = request_hash


def canonical_request_hash(messages: Sequence[ChatMessage], available_tool_names: Sequence[str]) -> str:
    canonical_messages = []
    for message in messages:
        canonical = {"role": message.role, "content": message.content,
                     "name": message.name, "tool_call_id": message.tool_call_id, "tool_calls": []}
        for call in message.tool_calls:
            tool = {"id": call.id, "name": call.name, "arguments": call.arguments}
            if call.raw_arguments:
                try:
                    tool["arguments"] = json.loads(call.raw_arguments)
                except json.JSONDecodeError:
                    # Malformed arguments are still an input, and must produce
                    # an honest replay miss rather than a JSON parser failure.
                    tool["invalid_raw_arguments"] = call.raw_arguments
            canonical["tool_calls"].append(tool)
        canonical_messages.append(canonical)
    payload = {"messages": canonical_messages, "available_tool_names": sorted(available_tool_names)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _message(raw: dict[str, Any]) -> ChatMessage:
    calls = []
    for call in raw.get("tool_calls", []):
        function = call.get("function", call)
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        calls.append(ToolCall(call["id"], function["name"], arguments))
    return ChatMessage(raw["role"], raw.get("content", ""), raw.get("name"), calls, raw.get("tool_call_id"))


class ReplayModelGateway:
    mode = "replay"
    model = "recorded-replay"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._responses: dict[str, LLMResponse] = {}
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                key = canonical_request_hash([_message(message) for message in record["messages"]],
                                             record["available_tool_names"])
                if key in self._responses:
                    raise ValueError("Duplicate replay request")
                raw = record["response"]
                self._responses[key] = LLMResponse(
                    content=raw.get("content", ""),
                    tool_calls=[ToolCall(call["id"], call["name"], call.get("arguments", {}), call.get("raw_arguments", ""))
                                for call in raw.get("tool_calls", [])],
                    finish_reason=FinishReason(raw.get("finish_reason", "stop")),
                    usage=Usage(**raw.get("usage", {})), model=raw.get("model", self.model),
                    raw={"mode": "replay", "request_hash": key},
                )
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ModelConfigurationError(f"Invalid replay recording: {self.path}") from exc

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] | None = None,
                 *, max_tokens: int = 1024) -> LLMResponse:
        key = canonical_request_hash(messages, [tool.name for tool in tools or []])
        if key not in self._responses:
            raise ReplayMissError(key)
        # Consumers mutate legacy response dataclasses; the stored response must
        # stay isolated so repeated calls return the actual recorded result.
        return copy.deepcopy(self._responses[key])
