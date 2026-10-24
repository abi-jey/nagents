"""Pure bounded formatting for untrusted child and human updates."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import NotRequired
from typing import TypedDict
from typing import cast

from nagents.harness.agent_updates import MAX_NOTIFICATION_BYTES
from nagents.harness.agent_updates import NOTIFICATION_PREFIX
from nagents.harness.agent_updates import WAKEUP_PREFIX
from nagents.harness.agent_updates import format_batch
from nagents.harness.agent_updates import format_wakeup
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskMessage

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nagents.harness.types import TaskCompleted as Completed
    from nagents.harness.types import TaskMessage as HumanMessage


class _SessionEntry(TypedDict):
    session_id: str
    status: str
    result: NotRequired[str]
    error: NotRequired[str]
    prompt: NotRequired[str]
    truncated: NotRequired[bool]


class _HumanEntry(TypedDict):
    session_id: str
    prompt: str
    truncated: NotRequired[bool]


class _BatchPayload(TypedDict):
    sessions: list[_SessionEntry]
    human_messages: list[_HumanEntry]
    more: NotRequired[int]


class _WakeupEntry(TypedDict):
    reason: str
    truncated: NotRequired[bool]


class _WakeupPayload(TypedDict):
    wakeups: list[_WakeupEntry]


def _payload(text: str) -> _BatchPayload:
    assert text.startswith(NOTIFICATION_PREFIX)
    payload: object = json.loads(text[len(NOTIFICATION_PREFIX) :])
    assert isinstance(payload, dict)
    sessions = payload.get("sessions")
    human_messages = payload.get("human_messages")
    assert isinstance(sessions, list) and isinstance(human_messages, list)
    for item in sessions:
        assert isinstance(item, dict)
        assert isinstance(item.get("session_id"), str) and isinstance(item.get("status"), str)
        assert (isinstance(item.get("result"), str)) != (isinstance(item.get("error"), str))
        assert set(item) <= {"session_id", "status", "result", "error", "truncated"}
        if "truncated" in item:
            assert item["truncated"] is True
    for item in human_messages:
        assert isinstance(item, dict)
        assert isinstance(item.get("session_id"), str) and isinstance(item.get("prompt"), str)
        assert set(item) <= {"session_id", "prompt", "truncated"}
    assert set(payload) <= {"sessions", "human_messages", "more"}
    if "more" in payload:
        assert type(payload["more"]) is int and payload["more"] > 0
    return cast("_BatchPayload", payload)


def _wakeup_payload(text: str) -> _WakeupPayload:
    assert text.startswith(WAKEUP_PREFIX)
    payload: object = json.loads(text[len(WAKEUP_PREFIX) :])
    assert isinstance(payload, dict)
    wakeups = payload.get("wakeups")
    assert isinstance(wakeups, list) and len(wakeups) == 1
    for item in wakeups:
        assert isinstance(item, dict) and isinstance(item.get("reason"), str)
        assert set(item) <= {"reason", "truncated"}
        if "truncated" in item:
            assert item["truncated"] is True
    assert set(payload) == {"wakeups"}
    return cast("_WakeupPayload", payload)


def _ids(events: Sequence[Completed | HumanMessage]) -> tuple[list[str], list[str]]:
    sessions = [event.session_id for event in events if isinstance(event, TaskCompleted)]
    humans = [event.session_id for event in events if isinstance(event, TaskMessage)]
    return sessions, humans


def test_format_single_result_and_mixed_update_shapes_without_private_fields() -> None:
    events: list[TaskCompleted | TaskMessage] = [
        TaskCompleted(session_id="s1", title="ignored", result="finished", status="completed"),
        TaskCompleted(session_id="s2", title="ignored", result="", error="provider failed", status="failed"),
        TaskCompleted(session_id="s3", title="ignored", result="", error="cancelled", status="cancelled"),
        TaskMessage(session_id="s4", title="ignored", prompt="please inspect this"),
    ]

    batch = format_batch(events)

    assert batch.count == 4
    assert len(batch.text.encode("utf-8")) <= MAX_NOTIFICATION_BYTES
    payload = _payload(batch.text)
    assert payload == {
        "sessions": [
            {"session_id": "s1", "status": "completed", "result": "finished"},
            {"session_id": "s2", "status": "failed", "error": "provider failed"},
            {"session_id": "s3", "status": "cancelled", "error": "cancelled"},
        ],
        "human_messages": [{"session_id": "s4", "prompt": "please inspect this"}],
    }
    assert not {"title", "name", "task_id", "parent_session_id", "root_session_id"} & set(payload)


def test_preview_clips_utf8_safely_and_json_escapes_text_under_hard_cap() -> None:
    value = ('é"\\\n' * 300) + "after"
    batch = format_batch([TaskCompleted(session_id="s1", title="private", result=value)])

    assert batch.count == 1
    assert len(batch.text.encode("utf-8")) <= MAX_NOTIFICATION_BYTES
    payload = _payload(batch.text)
    entry = payload["sessions"][0]
    assert entry == {
        "session_id": "s1",
        "status": "completed",
        "result": value.encode("utf-8")[:512].decode("utf-8", "ignore"),
        "truncated": True,
    }
    assert len(entry["result"].encode("utf-8")) <= 512


def test_overflow_keeps_longest_prefix_and_allows_caller_to_continue() -> None:
    events: list[TaskCompleted | TaskMessage] = [
        TaskCompleted(session_id=f"s{index}", title="ignored", result="\x01" * 512) for index in range(8)
    ]

    first = format_batch(events)
    first_payload = _payload(first.text)
    assert 0 < first.count < len(events)
    assert first_payload["more"] == len(events) - first.count
    assert [item["session_id"] for item in first_payload["sessions"]] == [
        event.session_id for event in events[: first.count]
    ]
    assert len(first.text.encode("utf-8")) <= MAX_NOTIFICATION_BYTES

    consumed = first.count
    while consumed < len(events):
        following = format_batch(events[consumed:])
        following_payload = _payload(following.text)
        assert following.count > 0
        assert following_payload.get("more", 0) == len(events) - consumed - following.count
        assert [item["session_id"] for item in following_payload["sessions"]] == [
            event.session_id for event in events[consumed : consumed + following.count]
        ]
        assert len(following.text.encode("utf-8")) <= MAX_NOTIFICATION_BYTES
        consumed += following.count


def test_empty_batch_is_empty_and_mixed_selection_follows_source_prefix() -> None:
    assert format_batch([]).text == ""
    assert format_batch([]).count == 0

    events: list[TaskCompleted | TaskMessage] = [
        TaskMessage(session_id="s1", title="ignored", prompt="first"),
        TaskCompleted(session_id="s2", title="ignored", result="second"),
        TaskMessage(session_id="s3", title="ignored", prompt="third"),
    ]
    batch = format_batch(events)
    payload = _payload(batch.text)
    assert batch.count == len(events)
    assert _ids(events[: batch.count]) == (
        [item["session_id"] for item in payload["sessions"]],
        [item["session_id"] for item in payload["human_messages"]],
    )


def test_batch_replaces_invalid_surrogates_and_marks_changed_content() -> None:
    batch = format_batch([TaskCompleted(session_id="s1", title="ignored", result="before\ud800after")])

    payload = _payload(batch.text)
    assert payload["sessions"][0] == {
        "session_id": "s1",
        "status": "completed",
        "result": "before?after",
        "truncated": True,
    }
    assert "\ud800" not in batch.text
    assert len(batch.text.encode("utf-8")) <= MAX_NOTIFICATION_BYTES


def test_wakeup_clips_unicode_and_json_escaped_reason_to_total_byte_cap() -> None:
    reason = ('é"\\\n\x01' * 3000) + "終わり"

    text = format_wakeup(reason)

    assert len(text.encode("utf-8")) <= MAX_NOTIFICATION_BYTES
    payload = _wakeup_payload(text)
    entry = payload["wakeups"][0]
    assert entry["truncated"] is True
    assert reason.startswith(entry["reason"])
    assert entry["reason"].encode("utf-8") == reason.encode("utf-8")[: len(entry["reason"].encode("utf-8"))]
    assert set(entry) == {"reason", "truncated"}
    next_reason = entry["reason"] + reason[len(entry["reason"])]
    one_more = WAKEUP_PREFIX + json.dumps(
        {"wakeups": [{"reason": next_reason, "truncated": True}]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert len(one_more.encode("utf-8")) > MAX_NOTIFICATION_BYTES


def test_wakeup_replaces_invalid_surrogate_and_marks_changed_reason() -> None:
    text = format_wakeup("before\ud800after")

    assert _wakeup_payload(text) == {"wakeups": [{"reason": "before?after", "truncated": True}]}
    assert "\ud800" not in text
    assert len(text.encode("utf-8")) <= MAX_NOTIFICATION_BYTES


def test_wakeup_short_reason_is_not_marked_truncated() -> None:
    assert _wakeup_payload(format_wakeup("Check for a completed run.")) == {
        "wakeups": [{"reason": "Check for a completed run."}]
    }
