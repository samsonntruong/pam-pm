"""Model client behind one interface.

Default is the Meta Responses API POST {base}/v1/responses with fallback
to POST {base}/v1/chat/completions on 404. OpenAI compatible bearer auth,
tool_choice auto only, retry with backoff on 429, at most 4 parallel
calls, usage logged via core.cost, and outbound text masked first.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from pam_pm.core import cost as cost_mod
from pam_pm.core.cost import CallUsage
from pam_pm.core.masking import mask_text

DEFAULT_MAX_TOKENS = 4000
MAX_PARALLEL = 4

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+")


@dataclass
class ChatToolCall:
    """One tool call requested by the model."""

    name: str
    arguments: str = ""
    call_id: str = ""


@dataclass
class ChatResult:
    """Text, tool calls, and usage for one model call."""

    text: str
    tool_calls: list[ChatToolCall] = field(default_factory=list)
    usage: CallUsage | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class ModelClientError(RuntimeError):
    """Model call failure with the API key already redacted."""


def split_base(base_url: str) -> tuple[str, str]:
    """Return (responses_url, chat_url) for a base URL with or without /v1."""
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return base + "/responses", base + "/chat/completions"
    return base + "/v1/responses", base + "/v1/chat/completions"


def tier_for(model: str, tier: str | None = None) -> str:
    """Return the explicit tier, else contributor when the model says so."""
    if tier:
        return tier
    if "contributor" in (model or "").lower():
        return "contributor"
    return "standard"


def _cfg_value(config: Any, *names: str) -> Any:
    if config is None:
        return None
    if isinstance(config, Mapping):
        for name in names:
            if config.get(name) is not None:
                return config[name]
        return None
    for name in names:
        if hasattr(config, name):
            value = getattr(config, name)
            if value is not None:
                return value
    return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_usage(payload: dict[str, Any]) -> tuple[int, int, int, int]:
    """Return (prompt, completion, cached, reasoning) token counts."""
    usage = payload.get("usage", {})
    if not isinstance(usage, dict):
        return 0, 0, 0, 0
    prompt = _as_int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
    completion = _as_int(
        usage.get("completion_tokens", usage.get("output_tokens", 0))
    )
    cached = 0
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(key, {})
        if isinstance(details, dict) and "cached_tokens" in details:
            cached = _as_int(details.get("cached_tokens"))
            break
    else:
        if "cached_tokens" in usage:
            cached = _as_int(usage.get("cached_tokens"))
    reasoning = 0
    for key in ("completion_tokens_details", "output_tokens_details"):
        details = usage.get(key, {})
        if isinstance(details, dict) and "reasoning_tokens" in details:
            reasoning = _as_int(details.get("reasoning_tokens"))
            break
    return prompt, completion, cached, reasoning


def mask_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy messages with mask_text applied to outbound text parts only."""
    masked: list[dict[str, Any]] = []
    for msg in messages:
        item = dict(msg)
        content = item.get("content")
        if isinstance(content, str):
            item["content"] = mask_text(content)
        elif isinstance(content, list):
            parts: list[Any] = []
            for part in content:
                if not isinstance(part, dict):
                    parts.append(part)
                    continue
                one = dict(part)
                if one.get("type") in ("text", "input_text") and isinstance(
                    one.get("text"), str
                ):
                    one["text"] = mask_text(str(one["text"]))
                parts.append(one)
            item["content"] = parts
        masked.append(item)
    return masked


def _tool_def(tool: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    fn = tool.get("function")
    if isinstance(fn, dict):
        return (
            str(fn.get("name", "")),
            str(fn.get("description", "")),
            dict(fn.get("parameters", {}) or {}),
        )
    params = tool.get("parameters", {})
    return (
        str(tool.get("name", "")),
        str(tool.get("description", "")),
        dict(params) if isinstance(params, dict) else {},
    )


class ModelClient:
    """One client for Responses API with chat fallback.

    Model and tier resolve from explicit args, then a config object or
    mapping (model, tier), then META_MODEL / META_TIER env vars.
    """

    def __init__(
        self,
        model: str | None = None,
        tier: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = DEFAULT_MAX_TOKENS,
        timeout: int = 60,
        retries: int = 3,
        backoff: float = 1.0,
        db: str | Path | sqlite3.Connection | None = None,
        config: Any = None,
    ) -> None:
        self.model = (
            model
            or _cfg_value(config, "model", "meta_model", "META_MODEL")
            or os.environ.get("META_MODEL", "")
        ).strip()
        want_tier = (
            tier
            or _cfg_value(config, "tier", "meta_tier", "META_TIER")
            or os.environ.get("META_TIER", "")
        ).strip()
        self.tier = tier_for(self.model, want_tier or None)
        self.base_url = (
            base_url
            or _cfg_value(
                config, "base_url", "meta_base_url", "BASE_URL", "META_BASE_URL"
            )
            or os.environ.get("META_BASE_URL", "")
        ).strip()
        self._api_key = (
            api_key
            or _cfg_value(
                config, "api_key", "meta_api_key", "META_API_KEY"
            )
            or os.environ.get("META_API_KEY", "")
        ).strip()
        cfg_tokens = _cfg_value(config, "max_tokens", "META_MAX_TOKENS")
        if max_tokens is None:
            max_tokens = (
                _as_int(cfg_tokens, DEFAULT_MAX_TOKENS)
                if cfg_tokens is not None
                else DEFAULT_MAX_TOKENS
            )
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._db = db
        self.last_usage: CallUsage | None = None
        self._sem = threading.BoundedSemaphore(MAX_PARALLEL)
        self._client: httpx.Client | None = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def responses_url(self) -> str:
        return split_base(self.base_url)[0]

    @property
    def chat_url(self) -> str:
        return split_base(self.base_url)[1]

    def _redact(self, text: object) -> str:
        out = _BEARER_RE.sub("Bearer [REDACTED]", str(text))
        if self._api_key and len(self._api_key) >= 4 and self._api_key in out:
            out = out.replace(self._api_key, "[REDACTED]")
        return out

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={"Authorization": "Bearer " + self._api_key},
            )
        return self._client

    def _build_responses(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content", "")
            parts: list[dict[str, Any]] = []
            if isinstance(content, str):
                parts.append({"type": "input_text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    kind = part.get("type")
                    if kind in ("text", "input_text"):
                        parts.append(
                            {
                                "type": "input_text",
                                "text": str(part.get("text", "")),
                            }
                        )
                    elif kind == "image_url":
                        inner = part.get("image_url", {})
                        url = (
                            inner.get("url", "")
                            if isinstance(inner, dict)
                            else str(inner)
                        )
                        parts.append(
                            {"type": "input_image", "image_url": url}
                        )
                    elif kind == "input_image":
                        parts.append(
                            {
                                "type": "input_image",
                                "image_url": str(part.get("image_url", "")),
                            }
                        )
            items.append(
                {"role": str(msg.get("role", "user")), "content": parts}
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "max_output_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": name,
                    "description": desc,
                    "parameters": params or {"type": "object"},
                }
                for name, desc, params in (_tool_def(t) for t in tools)
            ]
            payload["tool_choice"] = "auto"
        return payload

    def _build_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": params or {"type": "object"},
                    },
                }
                for name, desc, params in (_tool_def(t) for t in tools)
            ]
            payload["tool_choice"] = "auto"
        return payload

    def _post(self, url: str, payload: dict[str, Any]) -> tuple[int, dict]:
        try:
            resp = self._http().post(url, json=payload)
        except httpx.HTTPError as exc:
            raise ModelClientError(
                self._redact(f"request failed: {exc}")
            ) from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {"raw": str(data)[:2000]}
        return resp.status_code, data

    def _post_with_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> tuple[dict, bool]:
        url = self.responses_url
        payload = self._build_responses(messages, tools, max_tokens)
        used_chat = False
        attempt = 0
        while True:
            status, data = self._post(url, payload)
            if status == 429 and attempt < self.retries:
                time.sleep(self.backoff * (2**attempt))
                attempt += 1
                continue
            if status == 404 and not used_chat:
                used_chat = True
                url = self.chat_url
                payload = self._build_chat(messages, tools, max_tokens)
                attempt = 0
                continue
            if status != 200:
                detail = ""
                if isinstance(data, dict):
                    detail = str(data.get("error", data))[:500]
                raise ModelClientError(
                    self._redact(f"POST {url} status={status}: {detail}")
                )
            return data, used_chat

    @staticmethod
    def _parse_responses(data: dict) -> tuple[str, list[ChatToolCall]]:
        texts: list[str] = []
        calls: list[ChatToolCall] = []
        output = data.get("output", [])
        if not isinstance(output, list):
            return "", []
        for item in output:
            if not isinstance(item, dict):
                continue
            kind = item.get("type", "")
            if kind == "message":
                for part in item.get("content", []) or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in ("output_text", "text"):
                        texts.append(str(part.get("text", "")))
            elif kind == "function_call":
                calls.append(
                    ChatToolCall(
                        name=str(item.get("name", "")),
                        arguments=str(item.get("arguments", "")),
                        call_id=str(item.get("call_id", item.get("id", ""))),
                    )
                )
        return "".join(texts), calls

    @staticmethod
    def _parse_chat(data: dict) -> tuple[str, list[ChatToolCall]]:
        choices = data.get("choices", [])
        if not choices or not isinstance(choices[0], dict):
            return "", []
        msg = choices[0].get("message", {})
        if not isinstance(msg, dict):
            return "", []
        content = msg.get("content", "")
        if isinstance(content, list):
            text = "".join(
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict)
            )
        else:
            text = str(content or "")
        calls: list[ChatToolCall] = []
        for call in msg.get("tool_calls", []) or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function", {})
            if not isinstance(fn, dict):
                continue
            calls.append(
                ChatToolCall(
                    name=str(fn.get("name", "")),
                    arguments=str(fn.get("arguments", "")),
                    call_id=str(call.get("id", "")),
                )
            )
        return text, calls

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Send masked messages, returning text, tool calls, and usage."""
        if not self.model:
            raise ModelClientError("META_MODEL is missing.")
        if not self.base_url:
            raise ModelClientError("META_BASE_URL is missing.")
        if not self._api_key:
            raise ModelClientError("META_API_KEY is missing.")
        tokens = self.max_tokens if max_tokens is None else max_tokens
        if tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        masked = mask_messages(messages)
        with self._sem:
            data, used_chat = self._post_with_fallback(masked, tools, tokens)
        if used_chat:
            text, calls = self._parse_chat(data)
        else:
            text, calls = self._parse_responses(data)
        prompt, completion, cached, reasoning = extract_usage(data)
        usage = CallUsage(
            prompt_tokens=prompt,
            cached_tokens=cached,
            completion_tokens=completion,
            reasoning_tokens=reasoning,
            model=self.model,
            tier=self.tier,
        )
        if self._db is not None:
            cost_mod.log_call(self._db, usage)
        self.last_usage = usage
        return ChatResult(text=text, tool_calls=calls, usage=usage, raw=data)

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Send one user prompt with an optional system message."""
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return self.chat(msgs, tools=tools, max_tokens=max_tokens)
