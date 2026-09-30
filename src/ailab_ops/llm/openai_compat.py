"""Client for any OpenAI-compatible `/chat/completions` endpoint.

Deliberately written against `httpx` rather than any vendor SDK, because every
real deployment of this pattern ends up in the same place: a gateway that
speaks chat-completions, with an internal model name, an internal API key, and
no vendor SDK installed. vLLM, SGLang, TGI, lmdeploy, LiteLLM, one-api and most
enterprise model gateways all satisfy this contract.

Two things are handled more carefully than a naive client would:

* **Timeouts are split.** A connect timeout, a read timeout and a total
  deadline are different failures and need different handling. A read timeout
  on a streaming response means something different from a connect timeout, and
  conflating them is how "the model is slow" gets misdiagnosed as "the model is
  down".
* **Errors are typed.** `rate_limited` (429) is retryable with the server's
  `retry-after`; `quota` is not retryable at all; `server_error` (5xx) is
  retryable with backoff; `bad_request` (4xx other) is a bug on our side and
  retrying it is pure waste. The serving layer's retry logic branches on these,
  so the classification lives here rather than being re-derived from strings.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

import httpx

from .base import (
    ChatMessage,
    FinishReason,
    LLMResponse,
    ToolCall,
    ToolSpec,
    Usage,
    approx_tokens,
)


class OpenAICompatError(RuntimeError):
    """Raised for any non-retryable or exhausted backend failure."""

    def __init__(self, message: str, kind: str = "error", status: int | None = None,
                 retryable: bool = False, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retryable = retryable
        self.retry_after_s = retry_after_s


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def classify_status(status: int, body: str = "") -> tuple[str, bool, float | None]:
    """Map an HTTP status onto (kind, retryable, retry_after_s)."""
    low = body.lower()
    if status == 429:
        # Both live at 429 but they are operationally opposite: a rate limit
        # clears on its own, a quota does not.
        if "quota" in low or "usage limit" in low or "exceeded your current" in low:
            return "quota", False, None
        return "rate_limited", True, None
    if status in (500, 502, 503, 504):
        return "server_error", True, None
    if status == 408:
        return "timeout", True, None
    if status == 401:
        return "unauthorized", False, None
    if status == 403:
        return "forbidden", False, None
    if status == 404:
        return "not_found", False, None
    if 400 <= status < 500:
        return "bad_request", False, None
    return "error", False, None


@dataclass
class OpenAICompatClient:
    """Minimal chat-completions client."""

    base_url: str
    model: str
    api_key: str = "EMPTY"
    timeout_s: float = 60.0
    connect_timeout_s: float = 5.0
    max_retries: int = 2
    backoff_base_s: float = 0.4
    backoff_max_s: float = 8.0
    extra_headers: dict[str, str] | None = None

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._client: httpx.Client | None = None

    # ---- transport -----------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_s, connect=self.connect_timeout_s),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    **(self.extra_headers or {}),
                },
                # A loopback endpoint is by definition not reached through a
                # proxy, but httpx honours http_proxy for it anyway. On a
                # machine with the variable exported, every local request comes
                # back as a 503 from the proxy rather than from the server,
                # which looks exactly like a backend outage. Ignoring the
                # environment for loopback only is the narrow correct fix.
                trust_env=not _is_loopback(self.base_url),
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "OpenAICompatClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- payload -------------------------------------------------------

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] | None,
        temperature: float,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"
        if stream:
            payload["stream"] = True
        return payload

    # ---- retry ---------------------------------------------------------

    def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        attempt = 0
        last: OpenAICompatError | None = None
        while attempt <= self.max_retries:
            try:
                r = self.client.post("/chat/completions", json=payload)
            except httpx.ConnectTimeout as exc:
                last = OpenAICompatError(f"connect timeout: {exc}", kind="timeout", retryable=True)
            except httpx.ReadTimeout as exc:
                last = OpenAICompatError(f"read timeout after {self.timeout_s}s: {exc}", kind="timeout", retryable=True)
            except httpx.TransportError as exc:
                last = OpenAICompatError(f"transport error: {exc}", kind="transport", retryable=True)
            else:
                if r.status_code == 200:
                    try:
                        return r.json()
                    except json.JSONDecodeError as exc:
                        raise OpenAICompatError(
                            f"backend returned 200 with unparseable JSON: {exc}; "
                            f"first 200 bytes: {r.text[:200]!r}",
                            kind="protocol",
                        ) from exc
                kind, retryable, retry_after = classify_status(r.status_code, r.text)
                retry_after = retry_after or _retry_after_header(r)
                last = OpenAICompatError(
                    f"HTTP {r.status_code} ({kind}): {r.text[:300]}",
                    kind=kind,
                    status=r.status_code,
                    retryable=retryable,
                    retry_after_s=retry_after,
                )
                if not retryable:
                    raise last

            attempt += 1
            if attempt > self.max_retries:
                break
            delay = last.retry_after_s if (last and last.retry_after_s) else self._backoff(attempt)
            time.sleep(delay)

        raise last or OpenAICompatError("request failed for an unknown reason")

    def _backoff(self, attempt: int) -> float:
        # Exponential with jitter. The jitter is not decoration: without it,
        # every client that failed at the same instant retries at the same
        # instant, which is precisely how a retry storm turns one upstream
        # hiccup into a sustained outage.
        import random

        return min(self.backoff_base_s * (2 ** (attempt - 1)), self.backoff_max_s) * (0.5 + random.random())

    # ---- protocol ------------------------------------------------------

    def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        payload = self._payload(messages, tools, temperature, max_tokens, stream=False)
        raw = self._post_with_retry(payload)
        return self._parse_response(raw, messages, time.perf_counter() - t0)

    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield `{"type": "delta", ...}` and finally `{"type": "done", ...}`.

        Tool calls in a streamed response arrive as fragments spread over many
        chunks with an index, and the arguments arrive as a string that is only
        complete at the end. Accumulating them correctly is the single most
        common bug in a hand-written streaming client, so it is done explicitly
        here rather than being hidden behind a library.
        """
        t0 = time.perf_counter()
        payload = self._payload(messages, tools, temperature, max_tokens, stream=True)
        acc: dict[int, dict[str, Any]] = {}
        content_parts: list[str] = []
        finish = FinishReason.STOP
        usage = Usage()

        try:
            with self.client.stream("POST", "/chat/completions", json=payload) as r:
                if r.status_code != 200:
                    body = r.read().decode("utf-8", "replace")
                    kind, retryable, ra = classify_status(r.status_code, body)
                    raise OpenAICompatError(
                        f"HTTP {r.status_code} ({kind}): {body[:300]}",
                        kind=kind, status=r.status_code, retryable=retryable, retry_after_s=ra,
                    )
                for line in r.iter_lines():
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", "replace")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        # A malformed SSE frame must not kill the stream: log
                        # and continue is the correct behaviour, since the
                        # remaining frames are still usable.
                        continue
                    if chunk.get("usage"):
                        usage = _usage_from(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                            yield {"type": "delta", "text": delta["content"]}
                        for tc in delta.get("tool_calls") or []:
                            idx = int(tc.get("index", 0))
                            slot = acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                        fr = choice.get("finish_reason")
                        if fr:
                            finish = _finish_from(fr)
        except httpx.ReadTimeout as exc:
            raise OpenAICompatError(f"stream read timeout: {exc}", kind="timeout", retryable=True) from exc

        tool_calls = [
            ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"], arguments=_parse_args(slot["args"]),
                     raw_arguments=slot["args"])
            for i, slot in sorted(acc.items())
            if slot["name"]
        ]
        content = "".join(content_parts)
        if not usage.tokens_in:
            usage = Usage(
                tokens_in=sum(approx_tokens(m.text_for_prompt()) for m in messages),
                tokens_out=approx_tokens(content) + sum(approx_tokens(tc.raw_arguments) for tc in tool_calls),
            )
        yield {
            "type": "done",
            "response": LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=FinishReason.TOOL_CALLS if tool_calls else finish,
                usage=usage,
                model=self.model,
                latency_s=time.perf_counter() - t0,
            ),
        }

    def _parse_response(
        self, raw: dict[str, Any], messages: Sequence[ChatMessage], latency: float
    ) -> LLMResponse:
        choices = raw.get("choices") or []
        if not choices:
            raise OpenAICompatError(f"backend returned no choices: {str(raw)[:200]}", kind="protocol")
        choice = choices[0]
        msg = choice.get("message") or {}
        tool_calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{i}",
                    name=fn.get("name", ""),
                    arguments=_parse_args(args_raw),
                    raw_arguments=args_raw,
                )
            )
        content = msg.get("content") or ""
        usage = _usage_from(raw.get("usage")) if raw.get("usage") else Usage(
            tokens_in=sum(approx_tokens(m.text_for_prompt()) for m in messages),
            tokens_out=approx_tokens(content),
        )
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=_finish_from(choice.get("finish_reason") or "stop"),
            usage=usage,
            model=raw.get("model", self.model),
            latency_s=latency,
            raw=raw,
        )


# Alias kept because a lot of teams name this class after the serving stack.
VLLMClient = OpenAICompatClient


def _retry_after_header(r: httpx.Response) -> float | None:
    for name in ("retry-after", "x-ratelimit-reset-after"):
        v = r.headers.get(name)
        if not v:
            continue
        try:
            return float(v)
        except ValueError:
            continue
    return None


def _usage_from(u: dict[str, Any]) -> Usage:
    return Usage(tokens_in=int(u.get("prompt_tokens") or 0), tokens_out=int(u.get("completion_tokens") or 0))


def _finish_from(reason: str) -> FinishReason:
    return {
        "stop": FinishReason.STOP,
        "tool_calls": FinishReason.TOOL_CALLS,
        "function_call": FinishReason.TOOL_CALLS,
        "length": FinishReason.LENGTH,
    }.get(reason, FinishReason.STOP)


def _parse_args(raw: str) -> dict[str, Any]:
    """Tolerantly parse tool arguments.

    Real backends emit small variations: an empty string, a double-encoded
    JSON string, a trailing comma. A hard failure here would abort the whole
    run over a cosmetic problem, so the parse is defensive and the failure is
    surfaced to the model as a tool error it can retry.
    """
    if not raw or not raw.strip():
        return {}
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        try:
            v = json.loads(json.loads(raw))
        except Exception:
            return {"__raw__": raw, "__parse_error__": "arguments were not valid JSON"}
    if isinstance(v, dict):
        return v
    return {"__raw__": v}
