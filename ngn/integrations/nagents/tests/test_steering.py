from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.tools import ToolRegistry
from nagents.types import GenerationConfig
from nagents.types import Message
from nagents.types import ToolDefinition

from ngn_nagents.protocol import Emitter
from ngn_nagents.protocol import ProtocolError
from ngn_nagents.protocol import mapping
from ngn_nagents.protocol import read
from ngn_nagents.runtime import Definition
from ngn_nagents.runtime import HostExecutor
from ngn_nagents.runtime import Start
from ngn_nagents.runtime import run_agent
from ngn_nagents.steering import _Steering


class OfflineProvider(Provider):
    def __init__(self, rounds: list[list[Event]]) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "offline-only", "offline-only")
        self.rounds = rounds
        self.requests: list[list[Message]] = []

    async def verify_model(self, force: bool = False) -> bool:
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        self.requests.append(deepcopy(messages))
        for event in self.rounds[len(self.requests) - 1]:
            yield deepcopy(event)


def offer(boundary: object, position: int, prompt: str) -> dict[str, object]:
    return {
        "type": "steering_offer",
        "boundary_id": boundary,
        "input": {
            "input_id": "steer-" + uuid4().hex,
            "request_id": str(uuid4()),
            "position": position,
            "prompt": prompt,
        },
    }


def legacy_start(database: Path) -> dict[str, object]:
    return {
        "type": "start",
        "run": {
            "session_id": "legacy-session",
            "prompt": "initial",
            "system_prompt": "policy",
            "database": str(database),
            "provider": {
                "kind": "openai_compatible",
                "model": "offline-only",
                "api": "",
                "base_url": "",
                "api_key_env": "FIXTURE_KEY",
            },
            "tools": [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}],
            "max_tool_rounds": 10,
        },
    }


def enqueue(messages: asyncio.Queue[dict[str, object]]) -> Emitter:
    def emit(run: str, event: dict[str, object]) -> None:
        messages.put_nowait(event)

    return emit


class SteeringTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_steering_acknowledgment_defaults_false_and_requires_strict_bool(self) -> None:
        baseline = legacy_start(Path("/private/runtime.sqlite3"))
        self.assertFalse(Start.parse(baseline).steering)
        self.assertFalse(Start("s", "prompt", "policy", Path("/private/runtime.sqlite3"), {}, [], 10).steering)
        for enabled in [False, True]:
            message = deepcopy(baseline)
            message["run"] = {**mapping(message["run"]), "steering": enabled}
            self.assertIs(Start.parse(message).steering, enabled)
        invalid_values: list[object] = [None, 0, 1, 1.0, "true", [], {}]
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                message = deepcopy(baseline)
                message["run"] = {**mapping(message["run"]), "steering": invalid}
                with self.assertRaises(ProtocolError):
                    Start.parse(message)
        baseline["run"] = {**mapping(baseline["run"]), "steering_approved": True}
        with self.assertRaises(ProtocolError):
            Start.parse(baseline)

    async def test_legacy_host_start_finishes_tool_work_without_plugin_boundary_or_ack_channel(self) -> None:
        for acknowledgment in [{}, {"steering": False}]:
            with self.subTest(acknowledgment=acknowledgment), tempfile.TemporaryDirectory() as temporary:
                provider = OfflineProvider(
                    [
                        [ToolCallEvent(id="legacy-call", name="read_file", arguments={})],
                        [TextDoneEvent(text="ordinary final")],
                    ]
                )
                message = legacy_start(Path(temporary) / "runtime.sqlite3")
                message["run"] = {**mapping(message["run"]), **acknowledgment}
                start = Start.parse(message)
                messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                executor = HostExecutor(ToolRegistry(), "legacy-run", enqueue(messages))
                with self.assertRaises(ProtocolError):
                    executor.steering.accept(offer(str(uuid4()), 1, "unsolicited"))
                seen: list[str] = []
                with (
                    patch("ngn_nagents.runtime.create_provider", return_value=provider),
                    patch("ngn_nagents.runtime._SteeringPlugin") as plugin,
                ):
                    task = asyncio.create_task(run_agent(start, executor, executor.emit))
                    while True:
                        event = await asyncio.wait_for(messages.get(), 10)
                        seen.append(str(event["type"]))
                        self.assertNotIn(event["type"], {"steering_boundary", "steering_applied"})
                        if event["type"] == "tool_request":
                            executor.accept(
                                {
                                    "type": "tool_result",
                                    "result": {
                                        "call_id": "legacy-call",
                                        "name": "read_file",
                                        "result": "ordinary result",
                                        "error": "",
                                    },
                                }
                            )
                        elif event["type"] == "finished":
                            self.assertEqual(event["status"], "completed", event)
                            break
                    await asyncio.wait_for(task, 10)
                    plugin.assert_not_called()
                self.assertEqual(seen.count("tool_request"), 1)
                self.assertEqual(seen[-1], "finished")
                self.assertEqual(
                    [item.role for item in await SessionManager(start.database).get_history(start.session_id)],
                    ["user", "assistant", "tool", "assistant"],
                )

    async def test_duplicate_boundary_fields_are_rejected(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(
            b'{"protocol_version":1,"run_id":"run","message":{"type":"steering_offer","boundary_id":"a","boundary_id":"b","input":null}}\n'
        )
        with self.assertRaises(ProtocolError):
            await read(reader)

    async def test_complete_two_tool_batch_is_persisted_before_fifo_steering_and_ack(self) -> None:
        provider = OfflineProvider(
            [
                [ToolCallEvent(id=f"call-{index}", name="read_file", arguments={}) for index in range(2)],
                [TextDoneEvent(text="finished")],
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.sqlite3"
            start = Start(
                "s",
                "initial",
                "policy",
                path,
                {},
                [Definition("read_file", "read", {"type": "object"})],
                10,
                steering=True,
            )
            messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            executor = HostExecutor(ToolRegistry(), "run-exact", lambda run, message: messages.put_nowait(message))
            manager = SessionManager(path)
            with patch("ngn_nagents.runtime.create_provider", return_value=provider):
                task = asyncio.create_task(run_agent(start, executor, executor.emit))
                calls: list[str] = []
                applied: list[str] = []
                offered: list[str] = []
                boundaries: list[str] = []
                while True:
                    message = await asyncio.wait_for(messages.get(), 10)
                    if message["type"] == "tool_request":
                        call = message["call"]
                        assert isinstance(call, dict)
                        calls.append(str(call["call_id"]))
                        if len(calls) == 2:
                            history = await manager.get_history("s")
                            self.assertEqual([item.role for item in history], ["user", "assistant", "tool"])
                            self.assertFalse(boundaries, "No boundary is permitted before the last tool result")
                            self.assertTrue(messages.empty())
                        executor.accept(
                            {
                                "type": "tool_result",
                                "result": {
                                    "call_id": call["call_id"],
                                    "name": "read_file",
                                    "result": "ok",
                                    "error": "",
                                },
                            }
                        )
                    elif message["type"] == "steering_boundary":
                        boundaries.append(str(message["boundary_id"]))
                        history = await manager.get_history("s")
                        self.assertEqual([item.tool_call_id for item in history if item.role == "tool"], calls)
                        self.assertEqual(len(calls), 2)
                        if message["kind"] == "after_tools" and len(offered) < 2:
                            prompt = ["first steering", "second steering"][len(offered)]
                            offered.append(prompt)
                            executor.steering.accept(offer(message["boundary_id"], len(offered), prompt))
                        else:
                            executor.steering.accept(
                                {"type": "steering_offer", "boundary_id": message["boundary_id"], "input": None}
                            )
                    elif message["type"] == "steering_applied":
                        history = await manager.get_history("s")
                        applied.append(str(history[-1].content))
                        self.assertEqual(applied, offered)
                    elif message["type"] == "finished":
                        self.assertEqual(message["status"], "completed", message)
                        break
                await task
            self.assertEqual(applied, ["first steering", "second steering"])
            self.assertEqual(len(set(boundaries)), len(boundaries))
            self.assertEqual(
                [item.role for item in provider.requests[1]],
                ["system", "user", "assistant", "tool", "tool", "user", "user"],
            )

    async def test_final_boundary_continues_same_session_without_duplicate_user_message(self) -> None:
        provider = OfflineProvider([[TextDoneEvent(text="first final")], [TextDoneEvent(text="second final")]])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.sqlite3"
            start = Start("same-session", "initial", "policy", path, {}, [], 10, steering=True)
            messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            executor = HostExecutor(ToolRegistry(), "same-run", lambda run, message: messages.put_nowait(message))
            manager = SessionManager(path)
            boundaries = 0
            applied = 0
            with patch("ngn_nagents.runtime.create_provider", return_value=provider):
                task = asyncio.create_task(run_agent(start, executor, executor.emit))
                while True:
                    message = await asyncio.wait_for(messages.get(), 10)
                    if message["type"] == "steering_boundary":
                        self.assertEqual(message["kind"], "final")
                        history = await manager.get_history("same-session")
                        self.assertEqual(history[-1].role, "assistant", "Final response must already be persisted")
                        boundaries += 1
                        executor.steering.accept(
                            offer(message["boundary_id"], 1, "continue explicitly")
                            if boundaries == 1
                            else {"type": "steering_offer", "boundary_id": message["boundary_id"], "input": None}
                        )
                    elif message["type"] == "steering_applied":
                        applied += 1
                        history = await manager.get_history("same-session")
                        self.assertEqual(
                            [item.content for item in history if item.role == "user"],
                            ["initial", "continue explicitly"],
                        )
                    elif message["type"] == "finished":
                        self.assertEqual(message["status"], "completed", message)
                        break
                await task
            self.assertEqual((boundaries, applied, len(provider.requests)), (2, 1, 2))
            self.assertEqual(
                [item.content for item in provider.requests[1] if item.role == "user"],
                ["initial", "continue explicitly"],
            )

    async def test_offers_are_strict_boundary_bound_and_cancellation_does_not_ack(self) -> None:
        messages: list[dict[str, object]] = []
        steering = _Steering("run", lambda run, message: messages.append(message))
        task = asyncio.create_task(steering.boundary("final"))
        await asyncio.sleep(0)
        with self.assertRaises(ProtocolError):
            steering.accept(offer(str(uuid4()), 1, "foreign boundary"))
        with self.assertRaises(ProtocolError):
            steering.accept({**offer(messages[0]["boundary_id"], 1, "extra"), "approved": True})
        with self.assertRaises(ProtocolError):
            steering.accept(offer(messages[0]["boundary_id"], 1, "é" * 16_001))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(steering.pending)
        self.assertFalse(steering.offered)
        self.assertEqual([message["type"] for message in messages], ["steering_boundary"])

    async def test_failed_continuation_persistence_never_acknowledges_or_retries(self) -> None:
        provider = OfflineProvider([[TextDoneEvent(text="first final")]])
        with tempfile.TemporaryDirectory() as temporary:
            start = Start("s", "initial", "policy", Path(temporary) / "runtime.sqlite3", {}, [], 10, steering=True)
            messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            executor = HostExecutor(ToolRegistry(), "run", lambda run, message: messages.put_nowait(message))
            original = SessionManager.add_message

            async def failing(manager: SessionManager, session: str, message: Message) -> int:
                if message.content == "never ack":
                    raise OSError("secret upstream payload must not be exposed")
                return await original(manager, session, message)

            with (
                patch("ngn_nagents.runtime.create_provider", return_value=provider),
                patch.object(SessionManager, "add_message", failing),
            ):
                task = asyncio.create_task(run_agent(start, executor, executor.emit))
                while True:
                    message = await asyncio.wait_for(messages.get(), 10)
                    self.assertNotEqual(message["type"], "steering_applied")
                    if message["type"] == "steering_boundary":
                        executor.steering.accept(offer(message["boundary_id"], 1, "never ack"))
                    elif message["type"] == "finished":
                        self.assertEqual(message["status"], "failed")
                        self.assertNotIn("secret", str(message))
                        break
                await task
            self.assertEqual(len(provider.requests), 1)


if __name__ == "__main__":
    unittest.main()
