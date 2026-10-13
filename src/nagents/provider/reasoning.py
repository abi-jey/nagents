"""Documented effort choices and bounded, exact-model catalog capabilities.

Unknown models and custom gateways retain their own defaults. Explicit portable
settings require a known route or catalog capability, rather than guessing that
every model accepts the same enum. Existing provider-specific configuration is
still available for deliberately authored gateway requests.
"""

from __future__ import annotations

import re
from typing import cast
from typing import get_args
from urllib.parse import urlsplit

from ..types import ReasoningEffort

EFFORTS: tuple[ReasoningEffort, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_STANDARD: tuple[ReasoningEffort, ...] = ("low", "medium", "high")
_EXTENDED: tuple[ReasoningEffort, ...] = (*_STANDARD, "xhigh", "max")


def native_openai_api(model: str, *, api: str = "auto", base_url: str = "") -> str:
    """Resolve native auto tool routing; explicit contracts/endpoints stay intact."""
    if api != "auto" or base_url:
        return api
    name = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
    # Sol/Luna Chat tools require disabled reasoning. Responses works for all
    # supported efforts; Astra and Sol 6.1 require Responses for tools entirely.
    return "responses" if name in {"gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"} else "chat_completions"


def validate_reasoning_effort(value: object) -> ReasoningEffort:
    if not isinstance(value, str) or value not in get_args(ReasoningEffort):
        raise ValueError("reasoning_effort must be default, none, minimal, low, medium, high, xhigh, or max")
    return cast("ReasoningEffort", value)


def validate_reasoning_choice(effort: ReasoningEffort, options: tuple[ReasoningEffort, ...]) -> None:
    validate_reasoning_effort(effort)
    if effort and effort not in options:
        raise ValueError(
            "This reasoning effort is not supported by the selected provider and model; choose provider default."
        )


def _openai(model: str) -> tuple[ReasoningEffort, ...]:
    model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
    if model in {"gpt-6-astra", "gpt-6.1-sol"}:
        return _EXTENDED
    if model in {"gpt-6-sol", "gpt-6-luna", "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}:
        return ("none", *_EXTENDED)
    if model in {"gpt-5.2", "gpt-5.4", "gpt-5.5"}:
        return ("none", *_STANDARD, "xhigh")
    if model == "gpt-5.1":
        return ("none", *_STANDARD)
    if model in {"gpt-5", "gpt-5-mini", "gpt-5-nano"}:
        return ("minimal", *_STANDARD)
    if model in {"o1", "o3", "o3-mini", "o4-mini", "gpt-5-codex", "gpt-5.1-codex", "gpt-5.1-codex-mini"}:
        return _STANDARD
    if model in {"gpt-5.1-codex-max", "gpt-5.2-codex", "gpt-5.3-codex"}:
        return (*_STANDARD, "xhigh")
    return ()


def _anthropic(model: str) -> tuple[ReasoningEffort, ...]:
    model = re.sub(r"-\d{8}$", "", model)
    if model in {"claude-opus-4-5", "claude-opus-4.5"}:
        return _STANDARD
    if model in {
        "claude-opus-4-6",
        "claude-opus-4.6",
        "claude-sonnet-4-6",
        "claude-sonnet-4.6",
        "claude-mythos-preview",
    }:
        return (*_STANDARD, "max")
    if re.fullmatch(r"claude-(?:opus-4[.-][78]|(?:opus|sonnet|fable|mythos)-5(?:[.-][15])?|haiku-5[.-]5)", model):
        return _EXTENDED
    return ()


def _gemini(model: str) -> tuple[ReasoningEffort, ...]:
    model = model.removesuffix("-preview")
    if model in {"gemini-3.8-flash", "gemini-3.1-pro"}:
        return _STANDARD
    if model == "gemini-3-pro":
        return ("low", "high")
    if model in {"gemini-3-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"}:
        return ("minimal", *_STANDARD)
    if model == "gemini-3.1-flash-lite-image":
        return ("minimal", "high")
    return ()


def reasoning_effort_options(
    provider_type: str, model: str, *, api: str = "auto", base_url: str = "", auth: str = ""
) -> tuple[ReasoningEffort, ...]:
    """Offline documented choices; a trusted exact catalog may replace these."""
    name = model.lower()
    host = urlsplit(base_url).hostname if base_url else ""
    if provider_type == "openai" and auth in {"chatgpt", "codex"}:
        return _openai(name) if not base_url and api == "auto" else ()
    if provider_type in {"openai", "openai_compatible"} and host in {"", "api.openai.com"}:
        return _openai(name) if api in {"auto", "responses", "chat_completions"} else ()
    if provider_type == "anthropic" and host in {"", "api.anthropic.com"} and api in {"auto", "messages"}:
        return _anthropic(name)
    if (
        provider_type in {"google", "gemini", "gemini_native"}
        and host in {"", "generativelanguage.googleapis.com"}
        and api == "auto"
    ):
        return _gemini(name.removeprefix("models/"))
    if (
        provider_type == "openrouter"
        and host in {"", "openrouter.ai"}
        and api in {"auto", "chat_completions", "responses"}
    ):
        family, _, identifier = name.partition("/")
        if family == "openai":
            return _openai(identifier)
        if family == "anthropic":
            return _anthropic(identifier)
        if family == "google":
            return _gemini(identifier)
    return ()


def catalog_reasoning_efforts(row: dict[str, object]) -> tuple[bool, tuple[ReasoningEffort, ...]]:
    """Only documented Codex/OpenRouter capability fields; no raw data retained."""
    if "supported_reasoning_levels" in row:
        raw = row["supported_reasoning_levels"]
        if not isinstance(raw, list) or len(raw) > 16:
            return False, ()
        values = [item.get("effort") for item in raw if isinstance(item, dict)]
        if len(values) != len(raw) or any(value not in EFFORTS for value in values):
            return False, ()
        return True, tuple(effort for effort in EFFORTS if effort in values)
    reasoning = row.get("reasoning")
    if not isinstance(reasoning, dict) or "supported_efforts" not in reasoning:
        return False, ()
    raw = reasoning["supported_efforts"]
    if raw is None:
        values = list(EFFORTS)
    elif isinstance(raw, list) and len(raw) <= 16 and all(value in EFFORTS for value in raw):
        values = raw
    else:
        return False, ()
    return True, tuple(
        effort
        for effort in EFFORTS
        if effort in values and not (effort == "none" and reasoning.get("mandatory") is True)
    )
