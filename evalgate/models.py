"""Calling a model: a real one over HTTP, or a deterministic stand-in.

The real call is an OpenAI-format chat completion, which is what an AI gateway and most
providers accept. The tools the application offers are sent with every task, so the
checks see the tool calls a model would really make.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from evalgate.golden import Output, Task, ToolCall

MAX_TOKENS = 400
Model = Callable[[str, Task], Output]


def parse_response(response: dict[str, Any]) -> Output:
    """An OpenAI-format chat completion → text, tool calls and token counts."""
    message = (response.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # content parts
        content = "".join(p.get("text") or "" for p in content if isinstance(p, dict))
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            args = {}  # a model that can't write JSON arguments fails the args check
        calls.append(
            ToolCall(str(function.get("name") or ""), args if isinstance(args, dict) else {})
        )
    usage = response.get("usage") or {}
    return Output(
        text=content if isinstance(content, str) else "",
        tool_calls=calls,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )


class HttpModel:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        tools: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._tools = tools or []
        auth = {"authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(
            timeout=timeout, transport=transport, headers={**(headers or {}), **auth}
        )

    def __call__(self, model: str, task: Task) -> Output:
        messages = [{"role": "system", "content": task.system}] if task.system else []
        messages.append({"role": "user", "content": task.prompt})
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": MAX_TOKENS}
        if self._tools:
            body["tools"] = self._tools
        started = time.perf_counter()
        try:
            resp = self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            # The task could not be run, which is not the same as the model failing it.
            return Output(error=f"could not be reached ({type(exc).__name__})")
        latency = round((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            # The status and the provider's error code, never the body: it can echo input.
            code = ""
            try:
                error = resp.json().get("error") or {}
                code = str(error.get("code") or error.get("type") or "")[:60]
            except (ValueError, AttributeError):
                pass
            return Output(error=f"HTTP {resp.status_code}" + (f" ({code})" if code else ""))
        try:
            out = parse_response(resp.json())
        except (ValueError, AttributeError, TypeError, IndexError):
            return Output(error="the response was not a chat completion")
        out.latency_ms = latency
        return out


def offline(model: str, task: Task) -> Output:
    """Deterministic stand-ins, so the harness and a golden file can be checked with no
    key and no spend. They say nothing about any real model.

    stub/baseline answers every task correctly. stub/regressed never calls a tool and
    does what an injected instruction says: what a worse model looks like. stub/costly
    is right, and slower and wordier."""
    wants = [c for c in task.checks if c.tool_call is not None]
    if model == "stub/regressed":
        # Still gets the plain answers right, which is what makes a regression easy to miss.
        right = " ".join(s for c in task.checks for s in c.contains or [])
        injected = " ".join(s for c in task.checks for s in c.not_contains or [])
        return Output(
            text=f"{right} {injected} I can't look that up.".strip(),
            prompt_tokens=40,
            completion_tokens=12,
            latency_ms=300,
        )
    heavy = model == "stub/costly"
    tokens, ms = (45, 450) if heavy else (30, 300)
    if wants:
        calls = [ToolCall(c.tool_call or "", dict(c.args or {})) for c in wants]
        return Output(tool_calls=calls, prompt_tokens=40, completion_tokens=tokens, latency_ms=ms)
    text = " ".join(s for c in task.checks for s in c.contains or []) or "Done."
    return Output(text=text, prompt_tokens=40, completion_tokens=tokens, latency_ms=ms)
