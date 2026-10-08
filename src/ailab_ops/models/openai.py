"""OpenAI-compatible completions with complete SSE tool-call accumulation."""

from __future__ import annotations

import json
import math
import time
from typing import Sequence

import httpx

from ailab_ops.llm.base import ChatMessage, FinishReason, LLMResponse, ToolCall, ToolSpec, Usage, approx_tokens
from ailab_ops.llm.openai_compat import (
    OpenAICompatClient, OpenAICompatError, _finish_from, _is_loopback,
    _parse_args, _retry_after_header, _usage_from, classify_status,
)
from .protocol import ModelBackendError, ModelConfigurationError, ModelRequestControl


class OpenAIModelGateway(OpenAICompatClient):
    """Reuse established parsing/backoff without changing the legacy client.

    Requests ask for SSE; compatible gateways returning ordinary JSON are
    accepted too. An injected client remains owned by the caller.
    """

    mode = "online"

    def __init__(self, base_url: str, model: str, api_key: str = "EMPTY", *,
                 client: httpx.Client | None = None, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 60.0, connect_timeout_s: float = 5.0,
                 max_retries: int = 2, backoff_base_s: float = 0.4,
                 retry_max_delay_s: float = 30.0):
        if client is not None and transport is not None:
            raise ValueError("Provide either client or transport")
        if not math.isfinite(retry_max_delay_s) or retry_max_delay_s < 0:
            raise ModelConfigurationError("retry_max_delay_s must be finite and non-negative")
        super().__init__(base_url=base_url, model=model, api_key=api_key, timeout_s=timeout_s,
                         connect_timeout_s=connect_timeout_s, max_retries=max_retries,
                         backoff_base_s=backoff_base_s)
        self._client = client
        self._owns_client = client is None
        self._transport = transport
        self.retry_max_delay_s = retry_max_delay_s

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout_s, connect=self.connect_timeout_s),
                transport=self._transport, trust_env=not _is_loopback(self.base_url),
            )
        return self._client

    def close(self) -> None:
        if self._owns_client:
            super().close()

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] | None = None,
                 *, max_tokens: int = 1024) -> LLMResponse:
        return self.complete_with_control(messages, tools, max_tokens=max_tokens)

    def complete_with_control(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] | None = None,
                              *, max_tokens: int = 1024,
                              control: ModelRequestControl | None = None) -> LLMResponse:
        payload = self._payload(messages, tools, 0.0, max_tokens, stream=True)
        payload["stream_options"] = {"include_usage": True}
        started = time.perf_counter()
        for attempt in range(self.max_retries + 1):
            remaining = control.remaining() if control else self.timeout_s
            timeout = httpx.Timeout(min(self.timeout_s, remaining),
                                    connect=min(self.connect_timeout_s, remaining))
            try:
                # Full URL also makes injected clients independent of base_url.
                with self.client.stream("POST", self.base_url + "/chat/completions", json=payload, timeout=timeout,
                        headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}) as response:
                    if response.status_code != 200:
                        body = response.read().decode("utf-8", "replace")
                        kind, retryable, _ = classify_status(response.status_code, body)
                        raise ModelBackendError(f"Model backend HTTP {response.status_code} ({kind})",
                            kind=kind, status=response.status_code, retryable=retryable,
                            retry_after_s=_retry_after_header(response))
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        result = self._collect_stream(response, messages)
                    else:
                        response.read()
                        result = self._parse_response(response.json(), messages, 0.0)
                    result.latency_s = time.perf_counter() - started
                    return result
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                phase = "connect" if isinstance(exc, httpx.ConnectTimeout) else (
                    "read" if isinstance(exc, httpx.ReadTimeout) else "write" if isinstance(exc, httpx.WriteTimeout) else "pool")
                error = ModelBackendError(f"Model backend {phase} timeout", kind="timeout", phase=phase, retryable=True)
            except httpx.TransportError as exc:
                phase = "connect" if isinstance(exc, httpx.ConnectError) else "read" if isinstance(exc, httpx.ReadError) else "transport"
                error = ModelBackendError(f"Model backend {phase} failure", kind="transport", phase=phase, retryable=True)
            except ModelBackendError as exc:
                error = exc
            except (OpenAICompatError, ValueError, TypeError, KeyError, AttributeError):
                # Never echo provider data or httpx exception text: either can
                # contain an authorization header or other request credentials.
                raise ModelBackendError("Invalid model backend response", kind="protocol") from None
            if control:
                # The in-flight operation may finish, but a cancelled/expired
                # investigation must not back off or create another request.
                control.remaining()
            hint = error.retry_after_s
            if hint is None or not math.isfinite(hint) or hint < 0:
                hint = self._backoff(attempt + 1) if error.retryable else None
            if hint is not None:
                hint = min(hint, self.retry_max_delay_s) if math.isfinite(hint) and hint >= 0 else 0.0
            # One normalized value governs both backoff and the public error.
            error.retry_after_s = hint
            if not error.retryable or attempt == self.max_retries:
                raise error from None
            if control:
                control.wait(error.retry_after_s)
            else:
                time.sleep(error.retry_after_s)
        raise ModelBackendError("Model request failed")

    def _collect_stream(self, response: httpx.Response, messages: Sequence[ChatMessage]) -> LLMResponse:
        slots: dict[int, dict[str, str]] = {}
        parts: list[str] = []
        finish = FinishReason.STOP
        usage: Usage | None = None
        saw_choice = False
        saw_terminal = False
        model = self.model
        for line in response.iter_lines():
            if line.startswith("event:") and line[6:].strip() == "error":
                raise ModelBackendError("Model backend emitted a stream error", kind="protocol", phase="stream")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("error") is not None:
                raise ModelBackendError("Model backend emitted a stream error", kind="protocol", phase="stream")
            model = chunk.get("model") or model
            if chunk.get("usage") is not None:
                usage = _usage_from(chunk["usage"])
            for choice in chunk.get("choices") or []:
                # complete() returns one completion, so choices must not mix.
                if choice.get("index", 0) != 0:
                    continue
                saw_choice = True
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    parts.append(delta["content"])
                for tool in delta.get("tool_calls") or []:
                    slot = slots.setdefault(int(tool.get("index", 0)), {"id": "", "name": "", "args": ""})
                    if tool.get("id"):
                        slot["id"] = tool["id"]
                    function = tool.get("function") or {}
                    if function.get("name"):
                        slot["name"] = function["name"]
                    slot["args"] += function.get("arguments") or ""
                if choice.get("finish_reason"):
                    reason = choice["finish_reason"]
                    if reason not in {"stop", "length", "error", "tool_calls", "function_call"}:
                        raise ModelBackendError("Invalid model backend stream finish reason", kind="protocol", phase="stream")
                    parsed = FinishReason.ERROR if reason == "error" else _finish_from(reason)
                    if finish not in {FinishReason.LENGTH, FinishReason.ERROR} or parsed == FinishReason.ERROR:
                        finish = parsed
                    saw_terminal = True
        if not saw_choice:
            raise ModelBackendError("Model backend stream contained no choices", kind="protocol", phase="stream")
        if not saw_terminal:
            raise ModelBackendError("Model backend stream ended before a terminal finish reason", kind="protocol", phase="stream")
        tool_calls = [ToolCall(slot["id"] or f"call_{index}", slot["name"], _parse_args(slot["args"]), slot["args"])
                      for index, slot in sorted(slots.items()) if slot["name"]]
        content = "".join(parts)
        if usage is None:
            usage = Usage(sum(approx_tokens(message.text_for_prompt()) for message in messages),
                          approx_tokens(content) + sum(approx_tokens(tool.raw_arguments) for tool in tool_calls))
        if tool_calls and finish not in {FinishReason.LENGTH, FinishReason.ERROR}:
            finish = FinishReason.TOOL_CALLS
        return LLMResponse(content=content, tool_calls=tool_calls,
                           finish_reason=finish,
                           usage=usage, model=model)
