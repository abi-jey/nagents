from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from nagents.tools import ToolRegistry
from nagents.types import ToolCall

from ngn_nagents.protocol import ProtocolError
from ngn_nagents.protocol import read
from ngn_nagents.runtime import HostExecutor


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_host_bound_and_results_cannot_be_swapped(self) -> None:
        emitted: list[dict[str, object]] = []
        executor = HostExecutor(ToolRegistry(), "run-1", lambda run, message: emitted.append(message))
        task = asyncio.create_task(
            executor.execute(ToolCall(id="call-1", name="read_file", arguments={"path": "README.md"}))
        )
        await asyncio.sleep(0)
        self.assertEqual(emitted[0]["type"], "tool_request")
        with self.assertRaises(ProtocolError):
            executor.accept(
                {"type": "tool_result", "result": {"call_id": "call-1", "name": "shell", "result": {}, "error": ""}}
            )
        executor.accept(
            {
                "type": "tool_result",
                "result": {"call_id": "call-1", "name": "read_file", "result": {"content": "hello"}, "error": ""},
            }
        )
        result = await task
        self.assertEqual(result.result, {"content": "hello"})
        self.assertIsNone(
            result.error, "Successful host results must remain successful in Agent.run's persisted/model context"
        )
        with self.assertRaises(ProtocolError):
            await executor.execute(ToolCall(id="call-1", name="read_file", arguments={}))

    async def test_protocol_is_versioned_and_strict(self) -> None:
        for extra in ({"protocol_version": 2}, {"unexpected": True}):
            value: dict[str, object] = {"protocol_version": 1, "run_id": "run-1", "message": {"type": "cancel"}}
            value.update(extra)
            reader = asyncio.StreamReader()
            reader.feed_data(json.dumps(value).encode() + b"\n")
            with self.assertRaises(ProtocolError):
                await read(reader)

    async def test_cancellation_cleans_up_pending_bindings(self) -> None:
        executor = HostExecutor(ToolRegistry(), "run-1", lambda run, message: None)
        task = asyncio.create_task(executor.execute(ToolCall(id="call-1", name="shell", arguments={})))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(executor.pending)

    async def test_adapter_uses_public_agent_boundary_without_python_harness(self) -> None:
        import sys
        from unittest.mock import patch

        from nagents import Provider
        from nagents import ProviderType

        from ngn_nagents.runtime import Start
        from ngn_nagents.runtime import create_agent

        with tempfile.TemporaryDirectory() as temporary:
            provider = Provider(ProviderType.OPENAI_COMPATIBLE, api_key="test", model="test")
            start = Start("ngn-test", "hello", "policy", Path(temporary) / "runtime.sqlite3", {}, [], 10)
            executor = HostExecutor(ToolRegistry(), "run-1", lambda run, message: None)
            with patch("ngn_nagents.runtime.create_provider", return_value=provider):
                agent = create_agent(start, executor)
            self.assertIs(agent.tool_executor, executor)
            self.assertFalse(agent.save_tool_outputs)
            self.assertFalse(
                any(name.startswith(("nagents.harness", "nagents.tui", "nagents.web")) for name in sys.modules)
            )
            await agent.close()


if __name__ == "__main__":
    unittest.main()
