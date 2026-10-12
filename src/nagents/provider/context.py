"""Exact-model context metadata and validated connection overrides."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Literal

import aiohttp

from ..adapters._validation import load_object

ContextWindowSource = Literal["configured", "provider", "heuristic"]
MAX_CONTEXT_WINDOW = 1_000_000_000
METADATA_TTL = 300.0
MAX_METADATA_MODELS = 10000


def validate_context_window(value: object, *, automatic: bool = True) -> int:
    minimum = 0 if automatic else 1
    if type(value) is not int or not minimum <= value <= MAX_CONTEXT_WINDOW:
        raise ValueError("context_window must be a positive integer token limit (or 0 for automatic)")
    return value


def validate_model_context_windows(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > 1024:
        raise ValueError("model_context_windows must be a bounded mapping of exact model IDs to token limits")
    result: dict[str, int] = {}
    for model, limit in value.items():
        if not isinstance(model, str) or not model.strip() or len(model) > 200 or not model.isprintable():
            raise ValueError("Invalid model ID in model_context_windows")
        result[model] = validate_context_window(limit, automatic=False)
    return result


def metadata_limit(value: object) -> int:
    """Ignore absent, boolean, fractional, or nonsensical upstream limits."""
    return value if type(value) is int and 0 < value <= MAX_CONTEXT_WINDOW else 0


@dataclass(frozen=True)
class ContextWindow:
    tokens: int
    source: ContextWindowSource


class ContextMetadata:
    """One connection's bounded exact-ID catalog; never shared across endpoints."""

    def __init__(self) -> None:
        self.identity: tuple[str, str, str] = ("", "", "")
        self.limits: dict[str, int] = {}
        self.retry_at: dict[str, float] = {}

    def copy(self) -> ContextMetadata:
        copied = ContextMetadata()
        copied.identity = self.identity
        copied.limits = dict(self.limits)
        copied.retry_at = dict(self.retry_at)
        return copied

    def bound(self) -> None:
        for values in (self.limits, self.retry_at):
            while len(values) > MAX_METADATA_MODELS:
                values.pop(next(iter(values)))

    def backoff(self, model: str) -> None:
        self.retry_at[model] = monotonic() + METADATA_TTL
        self.bound()

    def select(self, identity: tuple[str, str, str]) -> None:
        if self.identity != identity:
            self.identity = identity
            self.limits.clear()
            self.retry_at.clear()

    def update(self, rows: object, *, id_key: str = "id", nested: bool = False) -> None:
        if not isinstance(rows, list):
            return
        found: dict[str, int] = {}
        for row in rows[:MAX_METADATA_MODELS]:
            if not isinstance(row, dict):
                continue
            model = row.get(id_key)
            if not isinstance(model, str) or not model or len(model) > 200:
                continue
            self.limits.pop(model, None)
            info = row.get("model_info", {}) if nested else row
            if not isinstance(info, dict):
                continue
            limit = next(
                (
                    valid
                    for key in ("max_input_tokens", "inputTokenLimit", "context_window", "context_length")
                    if (valid := metadata_limit(info.get(key)))
                ),
                0,
            )
            # Gateway aliases can have several deployments: the shared safe
            # input limit is the smallest advertised value for that exact alias.
            if limit:
                found[model] = min(limit, found.get(model, limit))
        self.limits.update(found)
        self.retry_at.update({model: monotonic() + METADATA_TTL for model in found})
        self.bound()


async def fetch_context_metadata(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
    """Metadata never enters generation traces, cookies, redirects, or raw logs."""
    async with (
        aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False, cookie_jar=aiohttp.DummyCookieJar()
        ) as client,
        client.get(url, headers=headers, allow_redirects=False) as response,
    ):
        if response.status != 200:
            raise ValueError("Context metadata unavailable")
        body = bytearray()
        async for chunk in response.content.iter_chunked(8192):
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise ValueError("Context metadata exceeds its size limit")
        return load_object(body.decode("utf-8"))
