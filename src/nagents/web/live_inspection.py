"""Bounded, opt-in inspection of voice setup and delegated model requests."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from contextlib import suppress
from dataclasses import fields
from dataclasses import is_dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from nagents.observation import observer
from nagents.observation import scope

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

    from nagents.provider import Provider

Payload = dict[str, object]
MAX_MODEL_PAYLOAD = 64 * 1024
MAX_MODEL_BYTES = 256 * 1024
MAX_MODEL_REQUESTS = 32
MAX_CONTEXT_BYTES = 32 * 1024
MAX_CONTEXT_MESSAGES = 16
MAX_INSTRUCTION_BYTES = 16 * 1024
_ID = re.compile(r"[0-9a-f]{32}\Z")


def text_preview(value: str, limit: int) -> Payload:
    # Slice before UTF-8 encoding so even a very large source needs bounded scratch space.
    text = value[:limit].encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")
    return {"text": text, "characters": len(value), "truncated": text != value}


def model_preview(value: object, limit: int) -> Payload:
    """Copy a bounded JSON projection without recursively copying the entire request."""
    remaining, nodes, omitted = limit, 2048, False

    def project(item: object, depth: int = 0) -> object:
        nonlocal remaining, nodes, omitted
        nodes -= 1
        if remaining <= 0 or nodes <= 0 or depth > 24:
            omitted = True
            return "[Capture limit reached]"
        if isinstance(item, Enum):
            return project(item.value, depth + 1)
        if isinstance(item, datetime):
            return project(item.isoformat(), depth + 1)
        if is_dataclass(item) and not isinstance(item, type):
            item = {field.name: getattr(item, field.name) for field in fields(item)}
        if isinstance(item, str):
            preview = text_preview(item, remaining)
            text = str(preview["text"])
            remaining -= len(text.encode("utf-8"))
            omitted |= bool(preview["truncated"])
            return text
        if isinstance(item, dict):
            result: Payload = {}
            for key, child in item.items():
                if remaining <= 0 or nodes <= 0:
                    omitted = True
                    break
                if not isinstance(key, str):
                    omitted = True
                    continue
                projected_key = str(project(key, depth + 1))
                result[projected_key] = project(child, depth + 1)
            return result
        if isinstance(item, list | tuple):
            result_list: list[object] = []
            for child in item:
                if remaining <= 0 or nodes <= 0:
                    omitted = True
                    break
                result_list.append(project(child, depth + 1))
            return result_list
        if item is None or isinstance(item, bool | int | float):
            remaining -= 8
            return item
        omitted = True
        return "[Unsupported value]"

    text = json.dumps(project(value), ensure_ascii=False, indent=2, allow_nan=False)
    preview = text_preview(text, limit)
    if omitted:
        preview.update(truncated=True, characters=len(str(preview["text"])), characters_complete=False)
    return preview


def seed_details(instructions: str, history: tuple[Payload, ...]) -> Payload:
    """Retain only actual, whole text messages; omit malformed or over-budget items."""
    copied: list[Payload] = []
    used, omitted = 0, False
    for item in history[:MAX_CONTEXT_MESSAGES]:
        content = item.get("content")
        if (
            item.get("type") != "message"
            or item.get("role") not in {"user", "assistant"}
            or not isinstance(content, list)
            or len(content) != 1
            or not isinstance(content[0], dict)
            or content[0].get("type") not in {"input_text", "output_text"}
            or not isinstance(content[0].get("text"), str)
        ):
            omitted = True
            continue
        text = content[0]["text"]
        if len(text) > MAX_CONTEXT_BYTES:
            omitted = True
            continue
        message: Payload = {
            "type": "message",
            "role": item["role"],
            "content": [{"type": content[0]["type"], "text": text}],
        }
        size = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
        if used + size > MAX_CONTEXT_BYTES:
            omitted = True
            continue
        copied.append(message)
        used += size
    return {
        "available": True,
        "instructions": text_preview(instructions, MAX_INSTRUCTION_BYTES),
        "history": copied,
        "history_truncated": omitted or len(history) > MAX_CONTEXT_MESSAGES,
        "reason": "",
    }


def _generation_urls(provider: Provider) -> set[str]:
    # These are matched privately, never retained or returned to the browser.
    try:
        prefix = provider.base_url.rstrip("/")
        api_prefix = provider._api_prefix()
        model = provider.model
    except Exception:
        # Custom/unconfigured providers still expose their model_context boundary.
        return set()
    urls = {
        f"{api_prefix}/responses",
        f"{api_prefix}/chat/completions",
        f"{prefix}/messages",
        f"{prefix}/v1/messages",
        f"{prefix}/completions",
        f"{prefix}/models/{model}:generateContent",
        f"{prefix}/models/{model}:streamGenerateContent",
        f"{prefix}/deployments/{model}/chat/completions",
    }
    if prefix == "https://chatgpt.com/backend-api/codex/responses":
        urls.add(prefix)
    return urls


@contextmanager
def model_requests(session_id: str, provider: Provider, report: Callable[[Payload], None]) -> Iterator[None]:
    """Observe only this root's model boundary and its matching generation HTTP attempts."""
    previous = observer.get()
    active = True
    call_id = ""
    call_round = 0
    attempts: set[str] = set()
    count, used, limited = 0, 0, False
    urls = _generation_urls(provider)

    def capture(kind: str, data: Payload) -> None:
        nonlocal call_id, call_round, count, used, limited
        if not active or data.get("session_id") != session_id:
            return
        identifier, round_number = data.get("model_call_id"), data.get("round")
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            return
        if type(round_number) is not int or not 0 <= round_number <= 1_000_000:
            return
        if kind == "tool_started":
            call_id = ""
            attempts.clear()
            return
        if kind == "model_context":
            call_id, call_round = identifier, round_number
            attempts.clear()
        elif identifier != call_id or round_number != call_round:
            return
        attempt_id = data.get("attempt_id")
        if kind in {"http_response", "http_error"}:
            if isinstance(attempt_id, str):
                attempts.discard(attempt_id)
            return
        if kind == "http_request":
            if (
                data.get("method") == "POST"
                and data.get("url") in urls
                and isinstance(attempt_id, str)
                and _ID.fullmatch(attempt_id)
                and len(attempts) < MAX_MODEL_REQUESTS
            ):
                attempts.add(attempt_id)
            return
        if kind not in {"model_context", "http_request_body"}:
            return
        if kind == "http_request_body" and (attempt_id not in attempts or not isinstance(data.get("body"), str)):
            return
        if count >= MAX_MODEL_REQUESTS or used >= MAX_MODEL_BYTES:
            if not limited:
                limited = True
                report(
                    {
                        "type": kind,
                        "model_call_id": identifier,
                        "round": round_number,
                        "capture_limited": True,
                        **({"attempt_id": attempt_id, "segmented": True} if kind == "http_request_body" else {}),
                    }
                )
            return
        budget = min(MAX_MODEL_PAYLOAD, MAX_MODEL_BYTES - used)
        preview = (
            model_preview({key: data.get(key) for key in ("messages", "tools", "config")}, budget)
            if kind == "model_context"
            else text_preview(str(data["body"]), budget)
        )
        used += len(str(preview["text"]).encode("utf-8"))
        count += 1
        report(
            {
                "type": kind,
                "model_call_id": identifier,
                "round": round_number,
                "payload": preview,
                **({"attempt_id": attempt_id, "segmented": True} if kind == "http_request_body" else {}),
            }
        )

    def receive(kind: str, data: Payload) -> None:
        # Inspection must never alter generation, retry, or cleanup behavior.
        with suppress(Exception):
            capture(kind, data)
        if previous is not None:
            previous(kind, data)

    scope_token = scope.set(dict(scope.get()))
    token = observer.set(receive)
    try:
        yield
    finally:
        active = False
        attempts.clear()
        observer.reset(token)
        scope.reset(scope_token)
