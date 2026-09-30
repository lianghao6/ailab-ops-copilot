"""本地 OpenAI 兼容端点，由离线推理器支撑。

存在的意义：不依赖网络、GPU 和 API key，就能跑通 *真实* 的 HTTP 链路——
`OpenAICompatClient`、它的重试逻辑、SSE 解析、流式工具调用参数拼接。把
`AILAB_LLM_BACKEND=openai` 指向它，系统的行为与对接真实模型完全一致，包括各种
失败模式；这正是并发控制能被有效演示的前提。

它同时是一个故障注入器：`?fail_rate=0.3` 让它按比例返回 500，于是可以稳定复现
熔断器打开、降级阶梯掉到"仅证据"通道、以及系统自行恢复的完整过程。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, AsyncIterator

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .base import ChatMessage, ToolCall, ToolSpec
from .mock import MockLLMClient


def _to_messages(raw: list[dict[str, Any]]) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    for m in raw:
        tcs = []
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tcs.append(
                ToolCall(
                    id=tc.get("id") or "call_0",
                    name=fn.get("name", ""),
                    arguments=_loads(fn.get("arguments")),
                    raw_arguments=fn.get("arguments") or "{}",
                )
            )
        out.append(
            ChatMessage(
                role=m.get("role", "user"),
                content=_as_text(m.get("content")),
                name=m.get("name"),
                tool_calls=tcs,
                tool_call_id=m.get("tool_call_id"),
            )
        )
    return out


def _as_text(content: Any) -> str:
    """Normalise content to a string.

    Handles the multimodal case, where content is a list of parts. Getting this
    wrong is a common integration bug: a client sends `[{"type":"text",...}]`
    and the server does `content.strip()` on a list and dies.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
        return "\n".join(x for x in parts if x)
    return str(content)


def _loads(s: Any) -> dict[str, Any]:
    if isinstance(s, dict):
        return s
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def create_stub_app(
    model: str = "stub-reasoner-v1",
    fail_rate: float = 0.0,
    latency_s: float = 0.0,
    seed: int = 11,
) -> FastAPI:
    app = FastAPI(title="AILab Ops Copilot — local LLM stub", version="0.1.0")
    client = MockLLMClient(model=model)
    rng = random.Random(seed)

    @app.get("/v1/models")
    async def models() -> JSONResponse:
        return JSONResponse(content={"object": "list", "data": [{"id": model, "object": "model"}]})

    @app.post("/v1/chat/completions")
    async def chat(request: Request, fail_rate: float = Query(default=None)) -> Any:
        body = await request.json()
        rate = fail_rate if fail_rate is not None else globals_rate()
        if latency_s:
            await asyncio.sleep(latency_s)
        if rate > 0 and rng.random() < rate:
            # 503 is retryable and 429 is retryable-with-backoff; both are
            # exercised here so the client's classification is tested.
            code = rng.choice([500, 502, 503, 429])
            return JSONResponse(
                status_code=code,
                content={"error": {"message": f"injected failure ({code})", "type": "server_error"}},
                headers={"retry-after": "1"} if code == 429 else None,
            )

        messages = _to_messages(body.get("messages") or [])
        tools = [ToolSpec.from_openai(t) for t in (body.get("tools") or [])]
        if body.get("stream"):
            return StreamingResponse(_stream(client, messages, tools), media_type="text/event-stream")

        resp = client.chat(messages, tools=tools, temperature=body.get("temperature", 0.0))
        return JSONResponse(content=_as_response(resp, model))

    _RATE["value"] = fail_rate

    @app.post("/v1/admin/fail-rate")
    async def set_fail_rate(rate: float = Query(..., ge=0.0, le=1.0)) -> JSONResponse:
        globals_rate(rate)
        return JSONResponse(content={"fail_rate": rate})

    return app


_RATE: dict[str, float] = {"value": 0.0}


def globals_rate(value: float | None = None) -> float:
    if value is not None:
        _RATE["value"] = value
    return _RATE["value"]


def _as_response(resp: Any, model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": resp.content,
                    **(
                        {"tool_calls": [tc.to_openai() for tc in resp.tool_calls]}
                        if resp.tool_calls
                        else {}
                    ),
                },
                "finish_reason": "tool_calls" if resp.tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": resp.usage.tokens_in,
            "completion_tokens": resp.usage.tokens_out,
            "total_tokens": resp.usage.total,
        },
    }


async def _stream(client: MockLLMClient, messages: list[ChatMessage], tools: list[ToolSpec]) -> AsyncIterator[str]:
    """Emit OpenAI-style SSE frames.

    Note the tool-call framing: the first chunk carries the id and the function
    name, and the arguments arrive as a JSON *string fragment* that the client
    must concatenate. A client that reads `arguments` as an object works
    against a non-streaming endpoint and breaks against a streaming one -- which
    is exactly the bug this stub lets you reproduce on purpose.
    """
    resp = client.chat(messages, tools=tools)
    cid = f"chatcmpl-{int(time.time() * 1000)}"

    def frame(delta: dict[str, Any], finish: str | None = None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": client.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield frame({"role": "assistant", "content": ""})
    if resp.tool_calls:
        for i, tc in enumerate(resp.tool_calls):
            yield frame({"tool_calls": [{"index": i, "id": tc.id, "type": "function",
                                        "function": {"name": tc.name, "arguments": ""}}]})
            args = tc.raw_arguments or json.dumps(tc.arguments)
            step = 24
            for j in range(0, len(args), step):
                yield frame({"tool_calls": [{"index": i, "function": {"arguments": args[j:j + step]}}]})
            await asyncio.sleep(0)
        yield frame({}, "tool_calls")
    else:
        text = resp.content
        step = 48
        for i in range(0, len(text), step):
            yield frame({"content": text[i : i + step]})
            await asyncio.sleep(0)
        yield frame({}, "stop")

    usage = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": client.model,
        "choices": [],
        "usage": {
            "prompt_tokens": resp.usage.tokens_in,
            "completion_tokens": resp.usage.tokens_out,
            "total_tokens": resp.usage.total,
        },
    }
    yield f"data: {json.dumps(usage, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
