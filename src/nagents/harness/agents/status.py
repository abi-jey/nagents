"""Detached, bounded published evidence for read-only child-session status."""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import asdict
from typing import TYPE_CHECKING
from typing import Literal
from typing import TypeAlias
from typing import TypedDict

if TYPE_CHECKING:
    from .subagents import TaskInfo

MAX_HISTORY = 20
MAX_PREVIEW_BYTES = 1024
MAX_TEXT_BYTES = 2048
MAX_OUTPUT_BYTES = 1024
_MAX_COUNT = 2**63 - 1
_USAGE_KEYS = frozenset(
    {"prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "audio_tokens", "reasoning_tokens"}
)
_USAGE_FLAGS = {
    "prompt_tokens": "prompt_reported",
    "completion_tokens": "completion_reported",
    "total_tokens": "total_reported",
    "reasoning_tokens": "reasoning_reported",
}
_CONTEXT_COUNTS = frozenset(
    {
        "total_tokens",
        "context_window",
        "remaining_tokens",
        "observed_prompt_tokens",
        "observed_completion_tokens",
        "observed_reasoning_tokens",
        "observed_context_tokens",
        "pending_context_tokens",
        "context_tokens",
    }
)
_CONTEXT_TEXT = frozenset(
    {
        "provider",
        "model",
        "estimate_method",
        "context_window_source",
        "observed_reasoning_in_output",
        "context_tokens_source",
    }
)
_COMPACTION_COUNTS = frozenset({"token_threshold", "token_budget", "message_threshold"})
_BLOCKED_OUTPUT_KEYS = frozenset(
    {"arguments", "arguments_text", "extra", "provider_extra", "provider_extras", "reasoning", "thinking"}
)
_PHASES = frozenset(
    {
        "starting",
        "running",
        "thinking",
        "responding",
        "waiting",
        "waiting_approval",
        "waiting_input",
        "waiting_children",
        "waiting_delegation",
        "compacting",
        "stopping",
        "done",
        "completed",
        "failed",
        "cancelled",
    }
)


class AgentToolStatus(TypedDict):
    name: str
    status: str


class AgentHistoryRow(TypedDict):
    type: str
    preview: str
    name: str
    status: str
    truncated: bool


class AgentStatusRecord(TypedDict):
    session_id: str
    title: str
    status: Literal["running", "completed", "failed", "cancelled"]
    agent: str
    parent_session_id: str
    provider: str
    model: str
    phase: str
    context: dict[str, object]
    compaction: dict[str, object]
    usage: dict[str, int]
    last_text: str
    last_tool: AgentToolStatus
    last_output: str
    result: str
    error: str
    artifacts: list[dict[str, object]]
    history: list[AgentHistoryRow]
    truncated: bool


class AgentStatusCollection(TypedDict):
    sessions: list[AgentStatusRecord]
    more: int


AgentStatusResult: TypeAlias = AgentStatusRecord | AgentStatusCollection


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _clip(value: str, maximum: int, *, tail: bool = False) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return encoded.decode("utf-8"), False
    clipped = encoded[-maximum:] if tail else encoded[:maximum]
    return clipped.decode("utf-8", errors="ignore"), True


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_COUNT


def _result_preview(value: object) -> tuple[str, bool]:
    """Read published tool results without retaining arbitrary objects or arguments."""
    omitted = False
    nodes = 64
    unsupported = object()

    def copy(item: object, depth: int) -> object:
        nonlocal omitted, nodes
        nodes -= 1
        if nodes < 0 or depth > 4:
            omitted = True
            return unsupported
        if isinstance(item, str):
            text, shortened = _clip(item, MAX_PREVIEW_BYTES)
            omitted |= shortened
            return text
        if item is None or type(item) is bool:
            return item
        if type(item) is int and -_MAX_COUNT <= item <= _MAX_COUNT:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        if isinstance(item, dict):
            result: dict[str, object] = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 16 or nodes <= 0:
                    omitted = True
                    break
                if not isinstance(key, str) or key in _BLOCKED_OUTPUT_KEYS:
                    continue
                name, shortened = _clip(key, 64)
                omitted |= shortened
                projected = copy(child, depth + 1)
                if projected is not unsupported:
                    result[name] = projected
            return result
        if isinstance(item, list | tuple):
            values: list[object] = []
            for index, child in enumerate(item):
                if index >= 16 or nodes <= 0:
                    omitted = True
                    break
                projected = copy(child, depth + 1)
                if projected is not unsupported:
                    values.append(projected)
            return values
        return unsupported

    copied = copy(value, 0)
    if copied is unsupported:
        return "", omitted
    if isinstance(copied, str):
        return copied, omitted
    preview, shortened = _clip(json.dumps(copied, ensure_ascii=False, allow_nan=False), MAX_PREVIEW_BYTES)
    return preview, omitted or shortened


class SessionEvidence:
    """A bounded observation cache; it never reads or changes conversation history."""

    def __init__(self) -> None:
        self._last_text = ""
        self._last_output = ""
        self._last_tool: AgentToolStatus = {"name": "", "status": ""}
        self._usage: dict[str, int] = {}
        self._history: deque[AgentHistoryRow] = deque(maxlen=MAX_HISTORY)
        self._response_id = ""
        self._text_done = False
        self._tool_key: tuple[str, str, int] = ("", "", -1)
        self._history_key: tuple[str, object] = ("", "")
        self._truncated = False

    @property
    def last_text(self) -> str:
        return self._last_text

    @property
    def last_output(self) -> str:
        return self._last_output

    @property
    def last_tool(self) -> AgentToolStatus:
        return {**self._last_tool}

    @property
    def usage(self) -> dict[str, int]:
        return dict(self._usage)

    @property
    def history(self) -> list[AgentHistoryRow]:
        return [{**row} for row in self._history]

    @property
    def truncated(self) -> bool:
        return self._truncated

    def _remember(self, kind: str, preview: str, name: str, status: str, key: object, shortened: bool) -> None:
        preview, clipped = _clip(preview, MAX_PREVIEW_BYTES, tail=kind in {"text_chunk", "text_done", "tool_output"})
        row: AgentHistoryRow = {
            "type": kind,
            "preview": preview,
            "name": name,
            "status": status,
            "truncated": shortened or clipped,
        }
        coalesced = kind in {"text_chunk", "tool_output", "response_metadata", "tool_call_progress"}
        if self._history and coalesced and self._history_key == (kind, key):
            self._history[-1] = row
        else:
            self._truncated |= len(self._history) == MAX_HISTORY
            self._history.append(row)
        self._history_key = (kind, key)
        self._truncated |= row["truncated"]

    def observe(self, payload: dict[str, object]) -> None:
        kind = _text(payload.get("type"))
        if kind not in {
            "text_chunk",
            "text_done",
            "tool_call",
            "tool_call_progress",
            "tool_execution_started",
            "tool_result",
            "tool_output",
            "response_metadata",
            "status",
            "user_message",
        }:
            return
        shortened = payload.get("truncated") is True
        if kind in {"text_chunk", "text_done"}:
            response_id, _ = _clip(_text(payload.get("response_id")), 200)
            if kind == "text_chunk" and response_id and response_id == self._response_id and self._text_done:
                return
            if response_id != self._response_id or (self._text_done and kind == "text_chunk"):
                self._last_text = ""
            self._response_id = response_id
            text = _text(payload.get("text" if kind == "text_done" else "chunk"))
            self._last_text, clipped = _clip(
                text if kind == "text_done" else self._last_text + text, MAX_TEXT_BYTES, tail=True
            )
            self._text_done = kind == "text_done"
            self._remember(
                kind,
                self._last_text,
                "",
                "completed" if self._text_done else "responding",
                response_id,
                shortened or clipped,
            )
            return
        if kind == "response_metadata":
            usage = payload.get("usage")
            if isinstance(usage, dict) and payload.get("usage_reported") is not False:
                counts = {
                    key: value
                    for key in _USAGE_KEYS
                    if type(value := usage.get(key)) is int
                    and _count(value)
                    and usage.get(_USAGE_FLAGS.get(key, key + "_reported"), True) is True
                    and (key in _USAGE_FLAGS or value > 0 or usage.get(key + "_reported") is True)
                }
                if counts:
                    self._usage = counts
            status = _text(payload.get("status"))
            status = status if status in _PHASES else ""
            response_id, _ = _clip(_text(payload.get("response_id")), 200)
            self._remember(kind, "", "", status, response_id, shortened)
            return
        if kind in {"status", "user_message"}:
            preview = _text(payload.get("detail" if kind == "status" else "text"))
            status = _text(payload.get("phase")) if kind == "status" else ""
            self._remember(kind, preview, "", status if status in _PHASES else "", "", shortened)
            return
        name, clipped = _clip(_text(payload.get("name")), 128)
        shortened |= clipped
        identifier, _ = _clip(_text(payload.get("id")), 200)
        generation, _ = _clip(_text(payload.get("generation_id")), 200)
        index = payload.get("index")
        key = (identifier, generation, index if type(index) is int and 0 <= index < 1024 else -1)
        same_tool = key == self._tool_key
        if not same_tool:
            self._last_output = ""
        self._tool_key = key
        if not name:
            name = self._last_tool["name"] if same_tool else ""
        status = {"tool_call": "requested", "tool_execution_started": "running", "tool_output": "running"}.get(kind, "")
        preview = ""
        if kind == "tool_call_progress":
            status = {"streaming": "preparing", "ready": "ready", "abandoned": "abandoned"}.get(
                _text(payload.get("status")), "preparing"
            )
        elif kind == "tool_output":
            self._last_output, clipped = _clip(
                self._last_output + _text(payload.get("text")), MAX_OUTPUT_BYTES, tail=True
            )
            preview, shortened = self._last_output, shortened or clipped
        elif kind == "tool_result":
            error = _text(payload.get("error"))
            status = "failed" if error else "completed"
            preview, clipped = _result_preview(error if error else payload.get("result"))
            shortened |= clipped
            if preview:
                self._last_output, clipped = _clip(preview, MAX_OUTPUT_BYTES, tail=True)
                shortened |= clipped
        self._last_tool = {"name": name, "status": status}
        self._remember(kind, preview, name, status, key, shortened)


def _configuration(info: TaskInfo) -> tuple[dict[str, object], dict[str, object], bool]:
    shortened = False

    def compaction(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}
        trigger = _text(value.get("trigger"))
        return {key: item for key, item in value.items() if key in _COMPACTION_COUNTS and _count(item)} | (
            {"trigger": trigger} if trigger in {"auto", "tokens", "messages", "off"} else {}
        )

    context: dict[str, object] = {}
    for key in _CONTEXT_COUNTS:
        value = info.context.get(key)
        valid = _count(value) or (
            key == "remaining_tokens" and type(value) is int and -_MAX_COUNT <= value <= _MAX_COUNT
        )
        if key in info.context and (value is None or valid):
            context[key] = value
    for key in _CONTEXT_TEXT:
        value = info.context.get(key)
        if isinstance(value, str):
            context[key], clipped = _clip(value, 256)
            shortened |= clipped
    components = info.context.get("components")
    if isinstance(components, list | tuple):
        copied: list[dict[str, object]] = []
        shortened |= len(components) > 20
        for item in components[:20]:
            if isinstance(item, dict) and _count(item.get("tokens")):
                key, clipped = _clip(_text(item.get("key")), 64)
                label, label_clipped = _clip(_text(item.get("label")), 128)
                shortened |= clipped or label_clipped
                copied.append({"key": key, "label": label, "tokens": item["tokens"]})
        context["components"] = copied
    if "compaction" in info.context:
        context["compaction"] = compaction(info.context["compaction"])
    return context, compaction(info.compaction), shortened


def make_status(info: TaskInfo, evidence: SessionEvidence, history_limit: int = 6) -> AgentStatusRecord:
    """Project current retained state without exposing storage IDs or mutating it."""
    if type(history_limit) is not int or not 0 <= history_limit <= MAX_HISTORY:
        raise ValueError("history_limit must be between 0 and 20")
    context, compaction, shortened = _configuration(info)
    result, result_clipped = _clip(info.result, MAX_TEXT_BYTES)
    error, error_clipped = _clip(info.error, MAX_TEXT_BYTES)
    title, title_clipped = _clip(info.title, 512)
    agent, agent_clipped = _clip(info.agent_name or info.profile, 512)
    provider, provider_clipped = _clip(info.provider, 256)
    model, model_clipped = _clip(info.model, 1024)
    phase, phase_clipped = _clip(info.phase, 64)
    shortened |= title_clipped or agent_clipped or provider_clipped or model_clipped or phase_clipped
    history = evidence.history
    record: AgentStatusRecord = {
        "session_id": info.session_id,
        "title": title,
        "status": info.status,
        "agent": agent,
        "parent_session_id": info.parent_session_id,
        "provider": provider,
        "model": model,
        "phase": phase,
        "context": context,
        "compaction": compaction,
        "usage": evidence.usage,
        "last_text": evidence.last_text,
        "last_tool": evidence.last_tool,
        "last_output": evidence.last_output,
        "result": result,
        "error": error,
        "artifacts": [asdict(artifact) for artifact in info.artifacts],
        "history": history[-history_limit:] if history_limit else [],
        "truncated": evidence.truncated or shortened or result_clipped or error_clipped or len(history) > history_limit,
    }
    return record
