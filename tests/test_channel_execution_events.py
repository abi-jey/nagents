"""Offline source-scoped execution notices using the real Agent and tool executor."""

import asyncio
import json
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from nagents.channels import Channel
from nagents.channels import ChannelActivity
from nagents.channels import ChannelError
from nagents.channels import ChannelEvent
from nagents.channels import ChannelExecutionEvent
from nagents.channels import ChannelExecutionPhase
from nagents.channels import ChannelMessage
from nagents.channels import ChannelValue
from nagents.channels import dispatch_channel_execution_event
from nagents.channels import sanitize_channel_tool_arguments
from nagents.channels.store import Admission
from nagents.channels.store import InboxStore
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import ReasoningChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.extensions import AgentPlugin
from nagents.extensions import RunContext
from nagents.types import Message
from tests.test_channels_runtime import MemoryChannel
from tests.test_channels_runtime import OfflineProvider
from tests.test_channels_runtime import assert_no_runtime_tasks
from tests.test_channels_runtime import inbound
from tests.test_channels_runtime import make_agent
from tests.test_channels_runtime import notifications
from tests.test_channels_runtime import rows


class EventChannel(MemoryChannel):
    def __init__(self, name: str = "source", messages: tuple[ChannelMessage, ...] = ()) -> None:
        super().__init__(name, messages)
        self.events: list[ChannelExecutionEvent] = []

    async def on_event(self, event: ChannelExecutionEvent) -> None:
        assert self.closed == 0
        self.events.append(event)


def test_public_listen_routes_notices_to_origin_and_preserves_shared_history_and_tools(tmp_path: Path) -> None:
    async def drive() -> None:
        origin = replace(inbound("one", conversation="room-a"), thread_id="thread-a")
        second = replace(inbound("two", conversation="room-b"), thread_id="thread-b")

        class CheckingTarget(EventChannel):
            async def action(self, name: str, arguments: dict[str, ChannelValue]) -> dict[str, ChannelValue]:
                assert [(event.phase, event.call_id) for event in source.events if event.call_id == "edit-call"] == [
                    ("tool_requested", "edit-call"),
                ]
                return await super().action(name, arguments)

        source = EventChannel(messages=(origin, second))
        other = CheckingTarget("other")
        provider = OfflineProvider(
            (
                (ToolCallEvent(id="catalog-call", name="channel_list", arguments={}),),
                (
                    ToolCallEvent(
                        id="send-call",
                        name="channel_send",
                        arguments={"channel": "other", "destination": "remote-room", "text": "explicit message"},
                    ),
                    ToolCallEvent(
                        id="edit-call",
                        name="channel_action",
                        arguments={
                            "channel": "other",
                            "action": "edit",
                            "arguments": {"id": "remote-1", "text": "edit"},
                        },
                    ),
                ),
            )
        )
        agent = make_agent(tmp_path / "routes.db", provider).add_channel(source).add_channel(other)
        observed: list[ChannelEvent] = []

        async def observe(event: ChannelEvent) -> None:
            observed.append(event)

        try:
            await asyncio.wait_for(agent.listen("identity", on_event=observe), 3)
            assert [event.phase for event in source.events] == [
                "run_started",
                "tool_requested",
                "tool_completed",
                "tool_requested",
                "tool_requested",
                "tool_completed",
                "tool_completed",
                "completed",
                "run_started",
                "completed",
            ]
            first_run, second_run = source.events[:8], source.events[8:]
            for group, message in ((first_run, origin), (second_run, second)):
                assert {event.conversation_id for event in group} == {message.conversation_id}
                assert {event.destination for event in group} == {message.conversation_id}
                assert {event.thread_id for event in group} == {message.thread_id}
                assert {event.message_id for event in group} == {message.message_id}
                assert {event.session_id for event in group} == {"identity"}
                assert len({event.run_id for event in group}) == len({event.activation_id for event in group}) == 1
                assert group[0].run_id and group[0].activation_id
            assert first_run[0].run_id != second_run[0].run_id
            assert first_run[0].activation_id != second_run[0].activation_id
            assert [(event.call_id, event.tool_name) for event in first_run if event.phase == "tool_completed"] == [
                ("catalog-call", "channel_list"),
                ("send-call", "channel_send"),
                ("edit-call", "channel_action"),
            ]
            assert not any(event.tool_failed for event in source.events)
            assert other.events == [] and other.activities == []
            assert source.sent == [] and len(other.sent) == 1 and len(other.operations) == 1
            assert other.sent[0].destination == "remote-room"
            assert [item["conversation_id"] for item in notifications(provider.requests[-1].messages)] == [
                "room-a",
                "room-b",
            ]
            assert provider.maximum_active == 1 and len(provider.requests) == 4
            assert len([event for event in observed if isinstance(event.event, DoneEvent)]) == 2
            assert {event.channel for event in observed} == {"source"}
            assert source.activities == [
                ChannelActivity("room-a", True, "thread-a", "identity"),
                ChannelActivity("room-a", False, "thread-a", "identity"),
                ChannelActivity("room-b", True, "thread-b", "identity"),
                ChannelActivity("room-b", False, "thread-b", "identity"),
            ]
            assert all(row[2] == "completed" for row in await rows(agent.session.db_path))
            assert_no_runtime_tasks()
        finally:
            await agent.close()

    asyncio.run(drive())


def test_privacy_and_mutation_isolation_without_losing_local_observer_data(tmp_path: Path) -> None:
    async def drive() -> None:
        calls: list[str] = []
        credential = "Bearer test-only-credential"

        def private_tool(query: str) -> dict[str, str]:
            calls.append(query)
            return {"result": "PRIVATE-TOOL-RESULT"}

        class MutatingChannel(EventChannel):
            async def on_event(self, event: ChannelExecutionEvent) -> None:
                await super().on_event(event)
                if event.phase == "tool_requested":
                    assert event.tool_arguments == {"query": "[redacted]"}
                    event.tool_arguments["query"] = "connector mutation"

        source = MutatingChannel(messages=(inbound(),))
        provider = OfflineProvider(
            (
                (
                    ReasoningChunkEvent(chunk="PRIVATE-REASONING", extra={"system": "PRIVATE-SYSTEM"}),
                    ToolCallEvent(id="private-call", name="private_tool", arguments={"query": credential}),
                ),
                (TextDoneEvent(text="PRIVATE-ASSISTANT"),),
            )
        )
        agent = make_agent(tmp_path / "privacy.db", provider).add_channel(source)
        agent.system_prompt = "PRIVATE-SYSTEM"
        agent.register_tool(private_tool)
        local: list[ChannelEvent] = []

        async def observe(event: ChannelEvent) -> None:
            local.append(event)

        try:
            await agent.listen("identity", on_event=observe)
            encoded = json.dumps([asdict(event) for event in source.events])
            assert "PRIVATE-" not in encoded and credential not in encoded
            assert calls == [credential]
            assert all(
                value in repr(local)
                for value in (
                    "PRIVATE-REASONING",
                    "PRIVATE-TOOL-RESULT",
                    "PRIVATE-ASSISTANT",
                    credential,
                )
            )
            assert source.sent == []
            assert source.events[-1].phase == "completed"
        finally:
            await agent.close()

    asyncio.run(drive())


def test_read_file_arguments_are_visible_through_constructor_and_dispatch_in_real_listen(tmp_path: Path) -> None:
    async def drive() -> None:
        calls: list[tuple[str, int]] = []

        def read_file(path: str, start_line: int) -> str:
            """Offline file-tool stand-in; performs no file I/O."""
            calls.append((path, start_line))
            return "PRIVATE-FILE-CONTENTS"

        source = EventChannel(messages=(inbound(),))
        provider = OfflineProvider(
            ((ToolCallEvent(id="read-call", name="read_file", arguments={"path": "README.md", "start_line": 20}),),)
        )
        agent = make_agent(tmp_path / "visible-arguments.db", provider).add_channel(source)
        agent.register_tool(read_file)
        try:
            await agent.listen("identity")
            requested = next(event for event in source.events if event.phase == "tool_requested")
            assert requested.call_id == "read-call" and requested.tool_name == "read_file"
            assert requested.tool_arguments == {"path": "README.md", "start_line": 20}
            # A connector formatter can use the real path and line, not placeholders.
            rendered = ", ".join(f"{key}={value}" for key, value in requested.tool_arguments.items())
            assert rendered == "path=README.md, start_line=20"
            assert calls == [("README.md", 20)]
            assert "PRIVATE-FILE-CONTENTS" not in repr(source.events)
        finally:
            await agent.close()

    asyncio.run(drive())


def test_short_ordinary_arguments_preserve_nested_values_and_detached_copies() -> None:
    arguments: dict[str, ChannelValue] = {
        "path": "docs/API notes.md",
        "name": "read_file",
        "design": "design=plain",
        "query": "channel execution events",
        "command": "python -m pytest -q",
        "url": "https://example.invalid/docs?start_line=20",
        "unicode": "résumé.md",
        "empty": "",
        "boundary": "x" * 80,
        "nested": {"paths": ["README.md", "src/nagents/agent.py"], "count": 20, "enabled": True, "unset": None},
    }
    result = sanitize_channel_tool_arguments(arguments)
    assert result == arguments
    assert sanitize_channel_tool_arguments(result) == result
    nested = cast("dict[str, ChannelValue]", result["nested"])
    cast("list[ChannelValue]", nested["paths"])[0] = "changed.md"
    assert cast("dict[str, ChannelValue]", arguments["nested"])["paths"] == ["README.md", "src/nagents/agent.py"]


@pytest.mark.parametrize(
    "value",
    [
        "Bearer fake-test-value",
        "curl -H 'Authorization: bEaReR fake-test-value' example.invalid",
        "Basic ZmFrZTpkZW1v",
        "Digest username=demo, response=fake-test-value",
        "https://demo:fake-pass@example.invalid/file",
        "postgresql://demo:fake-pass@example.invalid/db",
        "//demo@example.invalid/path",
        "demo:fake-pass@example.invalid",
        "https%3A%2F%2Fdemo%3Afake-pass%40example.invalid",
        "https://example.invalid/?access_token=fake-test-value",
        "https://example.invalid/?X-Amz-Signature=fake-test-value",
        "?api_key%3Dfake-test-value",
        "?api_key%253Dfake-test-value",
        "--password 'fake-test-value'",
        "AWS_SECRET_ACCESS_KEY=fake-test-value command",
        '{"clientSecret": "fake-test-value"}',
        "curl --user demo:fake-pass example.invalid",
        "curl --user=demo:fake-pass example.invalid",
        "curl --proxy-user=demo:fake-pass example.invalid",
        "curl -u demo:fake-pass example.invalid",
        "curl --oauth2-bearer fake-test-value example.invalid",
        "use sk-proj-test-only-not-a-real-key here",
        "ghp_test_only_not_a_real_key",
        "github_pat_test_only_not_a_real_key",
        "glpat-test-only-not-a-real-key",
        "xoxb-test-only-not-a-real-key",
        "hf_test_only_not_a_real_key",
        "npm_test_only_not_a_real_key",
        "pypi-test-only-not-a-real-key",
        "sk_test_not_a_real_key",
        "whsec_test_only_not_a_real_key",
        "AIza" + "0" * 35,
        "AKIA" + "0" * 16,
        "SG.not_a_real_key.not_a_real_key",
        "ya29.not_a_real_key",
        "123456789:" + "fake_test_value_" * 2,
        "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.fake_signature",
        "prefix eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0. suffix",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    ],
)
def test_common_or_embedded_credentials_under_misleading_keys_are_redacted(value: str) -> None:
    # All examples are fabricated. Keep them short enough to exercise detection,
    # independently of the rule that omits overlong values.
    assert len(value) <= 80
    original: dict[str, ChannelValue] = {"data": value, "nested": {"values": [value, "README.md"]}}
    expected: dict[str, ChannelValue] = {
        "data": "[redacted]",
        "nested": {"values": ["[redacted]", "README.md"]},
    }
    assert sanitize_channel_tool_arguments(original) == expected
    assert (
        ChannelExecutionEvent("room", "session", "tool_requested", tool_arguments=original).tool_arguments == expected
    )
    assert original["data"] == value


@pytest.mark.parametrize(
    "key", ["api_key", "accessKeyId", "password", "session_key", "env", "credentials", "system_prompt", "tool_result"]
)
def test_sensitive_subtrees_are_redacted_even_when_the_values_look_ordinary(key: str) -> None:
    assert sanitize_channel_tool_arguments({key: {"path": "README.md", "start_line": 20}}) == {key: "[redacted]"}


def test_credential_shaped_argument_keys_are_not_displayed() -> None:
    assert sanitize_channel_tool_arguments({"ghp_test_only_not_a_real_key": "README.md"}) == {
        "[redacted key]": "[redacted]",
    }


@pytest.mark.parametrize(
    "value",
    [
        "README.md\nother.md",
        "README.md\rhidden",
        "read\tfile",
        "read\x00file",
        "\x1b[31mred",
        "read\x7ffile",
        "read\x85file",
        "read\u2028file",
        "read\u202efile",
        "read\u200bfile",
        "README.md%0Ahidden",
        "README.md%250Ahidden",
        "x" * 81,
        "ordinary prefix " + "x" * 80 + " Bearer fake-test-value",
        "Bearer " + "fake_test_value_" * 10000,
    ],
)
def test_controls_and_overlong_values_are_omitted_whole_never_prefix_truncated(value: str) -> None:
    assert sanitize_channel_tool_arguments({"data": value}) == {"data": "[redacted]"}


def test_short_unicode_strings_still_obey_the_total_json_byte_limit() -> None:
    result = sanitize_channel_tool_arguments({f"field_{index}": "😊" * 80 for index in range(16)})
    assert result == {"[truncated]": True}
    assert len(json.dumps(result, ensure_ascii=True).encode()) <= 4096


def test_argument_summary_bounds_cycles_invalid_json_and_private_subtrees() -> None:
    class NoStringification:
        def __str__(self) -> str:
            raise AssertionError("Do not stringify arbitrary tool values")

    cycle: list[object] = []
    cycle.append(cycle)
    arguments: dict[str, object] = {
        "query": "a private string of arbitrary size" * 10000,
        "nested": {"count": 3, "enabled": True, "nothing": None, "password": {"leak": 123}},
        "authorization": [123456],
        "system_prompt": {"hidden": 42},
        "tool_result": {"payload": 17},
        "number": float("nan"),
        "infinite": float("inf"),
        "huge": 2**10000,
        "object": NoStringification(),
        "PRIVATE KEY WITH SPACES\x00": "secret",
        "cycle": cycle,
    }
    result = sanitize_channel_tool_arguments(arguments)
    encoded = json.dumps(result, allow_nan=False)
    assert len(encoded.encode()) <= 4096
    assert "private string" not in encoded and "PRIVATE KEY" not in encoded
    assert "123456" not in encoded and "payload" not in encoded and "hidden" not in encoded
    assert result["nested"] == {"count": 3, "enabled": True, "nothing": None, "password": "[redacted]"}
    assert result["object"] == result["huge"] == result["number"] == result["infinite"] == "[redacted]"
    assert "[truncated]" in encoded
    assert sanitize_channel_tool_arguments(result) == result
    assert arguments["nested"] == {"count": 3, "enabled": True, "nothing": None, "password": {"leak": 123}}
    assert sanitize_channel_tool_arguments(NoStringification()) == {}
    wide = {f"field_{index}": {f"nested_{child}": [0] * 1000 for child in range(100)} for index in range(100)}
    assert len(json.dumps(sanitize_channel_tool_arguments(wide)).encode()) <= 4096


@pytest.mark.parametrize("failure", ["exception", "timeout", "raise-cancel", "self-cancel", "self-cancel-return"])
def test_connector_failure_never_fails_model_or_retries_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    monkeypatch.setattr("nagents.channels.runtime.EXECUTION_EVENT_TIMEOUT", 0.01)

    async def drive() -> None:
        active = 0

        class BrokenChannel(EventChannel):
            async def on_event(self, event: ChannelExecutionEvent) -> None:
                nonlocal active
                await super().on_event(event)
                active += 1
                try:
                    if failure == "timeout":
                        await asyncio.Event().wait()
                    elif failure == "raise-cancel":
                        raise asyncio.CancelledError
                    elif failure in ("self-cancel", "self-cancel-return"):
                        task = asyncio.current_task()
                        assert task is not None
                        task.cancel()
                        if failure == "self-cancel":
                            await asyncio.sleep(0)
                    else:
                        raise ChannelError("PRIVATE-HOOK-FAILURE", outcome_unknown=True)
                finally:
                    active -= 1

            async def close(self) -> None:
                assert active == 0
                await super().close()

        source = BrokenChannel(messages=(inbound(),))
        provider = OfflineProvider(((ToolCallEvent(id="list-call", name="channel_list"),),))
        agent = make_agent(tmp_path / "hook-failure.db", provider).add_channel(source)
        try:
            await asyncio.wait_for(agent.listen("identity"), 3)
            assert [event.phase for event in source.events] == [
                "run_started",
                "tool_requested",
                "tool_completed",
                "completed",
            ]
            assert len(provider.requests) == 2
            assert (await rows(agent.session.db_path))[0][2] == "completed"
            assert "PRIVATE-HOOK-FAILURE" not in repr(source.events) + caplog.text
            assert active == 0 and source.closed == 1
            assert_no_runtime_tasks()
        finally:
            await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("failure", ["provider", "before-run", "after-run", "observer"])
def test_failed_run_has_one_terminal_notice_even_when_done_precedes_cleanup_failure(
    tmp_path: Path, failure: str
) -> None:
    async def drive() -> None:
        class FailingPlugin(AgentPlugin):
            async def before_run(self, context: RunContext, message: Message) -> Message:
                if failure == "before-run":
                    raise RuntimeError("PRIVATE-BEFORE-RUN")
                return message

            async def after_run(self, context: RunContext) -> None:
                if failure == "after-run":
                    raise RuntimeError("PRIVATE-AFTER-RUN")

        source = EventChannel(messages=(inbound(),))
        provider = OfflineProvider(
            ((ErrorEvent(message="PRIVATE-PROVIDER", recoverable=False),),) if failure == "provider" else ()
        )
        agent = make_agent(tmp_path / "failed.db", provider, FailingPlugin()).add_channel(source)
        done_seen = False

        async def observe(event: ChannelEvent) -> None:
            nonlocal done_seen
            done_seen |= isinstance(event.event, DoneEvent)
            if failure == "observer":
                raise RuntimeError("PRIVATE-OBSERVER")

        try:
            with pytest.raises((ChannelError, RuntimeError, BaseExceptionGroup)):
                await asyncio.wait_for(agent.listen("identity", on_event=observe), 3)
            assert [event.phase for event in source.events] == ["run_started", "failed"]
            assert "PRIVATE-" not in repr(source.events)
            assert (await rows(agent.session.db_path))[0][2] == "failed"
            assert failure != "after-run" or done_seen
            assert source.closed == 1 and not source.sent
        finally:
            await agent.close()

    asyncio.run(drive())


def test_tool_error_is_completed_attempt_not_success_or_run_failure_and_is_not_retried(tmp_path: Path) -> None:
    async def drive() -> None:
        source = EventChannel(messages=(inbound(),))
        source.failure_stage = "send"
        source.failure = ChannelError("PRIVATE-UNKNOWN-DELIVERY", outcome_unknown=True)
        provider = OfflineProvider(
            (
                (
                    ToolCallEvent(
                        id="uncertain",
                        name="channel_send",
                        arguments={
                            "channel": "source",
                            "destination": "room",
                            "text": "explicit",
                        },
                    ),
                ),
            )
        )
        agent = make_agent(tmp_path / "uncertain.db", provider).add_channel(source)
        try:
            await agent.listen("identity")
            result = next(event for event in source.events if event.phase == "tool_completed")
            assert result.tool_failed and result.call_id == "uncertain" and result.tool_name == "channel_send"
            assert result.tool_arguments == {} and "PRIVATE-" not in repr(source.events)
            assert source.events[-1].phase == "completed" and len(source.sent) == 1
            assert len(provider.requests) == 2
        finally:
            await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("cancel_at", ["run_started", "tool_requested", "provider", "tool"])
def test_cancellation_joins_hook_and_terminal_notice_before_close_without_cross_chat_or_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_at: str
) -> None:
    monkeypatch.setattr("nagents.channels.runtime.EXECUTION_EVENT_TIMEOUT", 0.05)

    async def drive() -> None:
        started = asyncio.Event()
        terminal = asyncio.Event()
        release_terminal = asyncio.Event()
        active = 0

        class SlowChannel(EventChannel):
            async def on_event(self, event: ChannelExecutionEvent) -> None:
                nonlocal active
                await super().on_event(event)
                active += 1
                try:
                    if event.phase == cancel_at:
                        started.set()
                        await asyncio.Event().wait()
                    if event.phase == "cancelled":
                        terminal.set()
                        await release_terminal.wait()
                finally:
                    active -= 1

            async def close(self) -> None:
                assert active == 0
                await super().close()

        message = replace(inbound("active"), thread_id="thread")
        source = SlowChannel(messages=(message, replace(message, message_id="queued")))
        other = EventChannel("other")
        provider = OfflineProvider(
            (
                (
                    ToolCallEvent(
                        id="send-call",
                        name="channel_send",
                        arguments={
                            "channel": "source",
                            "destination": "room",
                            "text": "explicit",
                        },
                    ),
                ),
            )
        )
        if cancel_at == "provider":
            provider.release.clear()
        if cancel_at == "tool":
            source.send_release.clear()
        agent = make_agent(tmp_path / "cancelled.db", provider).add_channel(source).add_channel(other)
        baseline = asyncio.all_tasks()
        listener = asyncio.create_task(agent.listen("identity"))
        try:
            waiting = (
                provider.started if cancel_at == "provider" else source.send_started if cancel_at == "tool" else started
            )
            await asyncio.wait_for(waiting.wait(), 3)
            await asyncio.wait_for(source.listen_finished.wait(), 3)
            listener.cancel()
            await asyncio.wait_for(terminal.wait(), 3)
            listener.cancel()
            await asyncio.sleep(0)
            assert source.closed == 0 and active == 1
            release_terminal.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(listener, 3)
            phases = [event.phase for event in source.events]
            assert phases[0] == "run_started" and phases[-1] == "cancelled"
            assert "tool_completed" not in phases and "completed" not in phases and "failed" not in phases
            assert len(source.sent) == (1 if cancel_at == "tool" else 0)
            assert all(event.conversation_id == "room" and event.thread_id == "thread" for event in source.events)
            assert all(event.message_id == "active" for event in source.events)
            assert other.events == [] and other.sent == []
            assert [(row[1], row[2]) for row in await rows(agent.session.db_path)] == [
                ("active", "interrupted"),
                ("queued", "queued"),
            ]
            assert active == 0 and source.closed == 1 and provider.active == 0
            assert asyncio.all_tasks() <= baseline
        finally:
            release_terminal.set()
            await agent.close()

    asyncio.run(drive())


def test_host_helper_supports_approval_and_resanitizes_mutable_arguments() -> None:
    async def drive() -> None:
        source = EventChannel()
        phase: ChannelExecutionPhase = "waiting_for_approval"
        arguments: dict[str, ChannelValue] = {"nested": {"count": 2}, "query": "README search"}
        event = ChannelExecutionEvent(
            "owning-room",
            "durable-session",
            phase,
            thread_id="owning-thread",
            run_id="run",
            activation_id="activation",
            call_id="call",
            tool_name="search",
            tool_arguments=arguments,
        )
        arguments["query"] = "changed original"
        assert event.tool_arguments["query"] == "README search"
        event.tool_arguments["credential"] = "PRIVATE-CREDENTIAL"
        event.tool_arguments["data"] = "Bearer fake-test-value"
        await dispatch_channel_execution_event(source, event)
        delivered = source.events[0]
        assert (
            delivered.phase == phase
            and delivered.destination == "owning-room"
            and delivered.thread_id == "owning-thread"
        )
        assert (delivered.session_id, delivered.run_id, delivered.activation_id, delivered.call_id) == (
            "durable-session",
            "run",
            "activation",
            "call",
        )
        assert delivered.tool_arguments["credential"] == "[redacted]"
        assert delivered.tool_arguments["data"] == "[redacted]"
        assert delivered.tool_arguments["query"] == "README search"
        cast("dict[str, ChannelValue]", delivered.tool_arguments["nested"])["count"] = 99
        assert event.tool_arguments["nested"] == {"count": 2}
        # Existing connectors can inherit the non-abstract hook without implementing it.
        assert MemoryChannel.on_event is Channel.on_event
        await dispatch_channel_execution_event(MemoryChannel("legacy"), event)
        for invalid in (replace(event, conversation_id=""), replace(event, thread_id="bad\x00thread")):
            await dispatch_channel_execution_event(source, invalid)
        assert len(source.events) == 1

    asyncio.run(drive())


@pytest.mark.parametrize("phase", ["run_started", "completed"])
def test_agent_close_joins_inflight_notices_and_does_not_reclassify_a_completed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    monkeypatch.setattr("nagents.channels.runtime.EXECUTION_EVENT_TIMEOUT", 0.05)

    async def drive() -> None:
        started = asyncio.Event()
        active = 0

        class SlowChannel(EventChannel):
            async def on_event(self, event: ChannelExecutionEvent) -> None:
                nonlocal active
                await super().on_event(event)
                active += 1
                try:
                    if event.phase == phase:
                        started.set()
                        await asyncio.Event().wait()
                finally:
                    active -= 1

            async def close(self) -> None:
                assert active == 0
                await super().close()

        source = SlowChannel(messages=(inbound(),))
        provider = OfflineProvider()
        agent = make_agent(tmp_path / "close.db", provider).add_channel(source)
        baseline = asyncio.all_tasks()
        listener = asyncio.create_task(agent.listen("identity"))
        try:
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.wait_for(agent.close(), 3)
            assert listener.cancelled() and source.closed == 1 and active == 0
            assert [event.phase for event in source.events] == [
                "run_started",
                "cancelled" if phase == "run_started" else "completed",
            ]
            assert (await rows(agent.session.db_path))[0][2] == (
                "interrupted" if phase == "run_started" else "completed"
            )
            assert len(provider.requests) == (0 if phase == "run_started" else 1)
            assert asyncio.all_tasks() <= baseline
        finally:
            await agent.close()

    asyncio.run(drive())


def test_legacy_inbox_without_route_executes_without_guessing_a_notice_destination(tmp_path: Path) -> None:
    async def drive() -> None:
        path = tmp_path / "legacy.db"
        store = InboxStore(path, "identity", 10)
        await store.initialize()
        assert await store.admit("source", "legacy", '{"message_id":"legacy"}') is Admission.INSERTED
        source = EventChannel()
        provider = OfflineProvider()
        agent = make_agent(path, provider).add_channel(source)
        try:
            await agent.listen("identity")
            assert source.events == [] and source.activities == [] and source.sent == []
            assert len(provider.requests) == 1 and (await rows(path))[0][2] == "completed"
        finally:
            await agent.close()

    asyncio.run(drive())
