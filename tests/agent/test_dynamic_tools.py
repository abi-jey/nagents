"""Tests for mid-run tool registration (dynamic tools).

Covers the per-round tool re-resolution in Agent.run(): a tool that
registers another tool during round N must make it callable in round N+1.
"""

import asyncio
from pathlib import Path
from typing import Any

from nagents import Agent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent


class TestMidRunToolRegistration:
    def test_tool_registered_mid_run_appears_next_round(self, tmp_path: Path, monkeypatch: Any) -> None:
        provider = Provider(
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            api_key="fake-key",
            model="fake-model",
        )

        async def _verify_model() -> bool:
            return True

        monkeypatch.setattr(provider, "verify_model", _verify_model)

        captured_tool_names: list[list[str]] = []

        async def fake_generate(
            messages: Any = None,
            tools: Any = None,
            config: Any = None,
            stream: bool = False,
        ) -> Any:
            captured_tool_names.append([t.name for t in tools] if tools else [])
            if len(captured_tool_names) == 1:
                yield ToolCallEvent(id="call-1", name="registrar", arguments={})
            else:
                yield TextDoneEvent(text="done")

        monkeypatch.setattr(provider, "generate", fake_generate)

        session = SessionManager(tmp_path / "sessions.db")
        agent = Agent(provider=provider, session_manager=session, tools=[])

        def new_tool() -> str:
            """A tool added while the run was in progress."""
            return "new"

        def registrar() -> str:
            """Register another tool with the live agent."""
            agent.register_tool(new_tool)
            return "ok"

        agent.register_tool(registrar)

        async def drive() -> None:
            async for _ in agent.run("go"):
                pass

        asyncio.run(drive())

        assert len(captured_tool_names) == 2
        assert "new_tool" not in captured_tool_names[0]
        assert "new_tool" in captured_tool_names[1]
