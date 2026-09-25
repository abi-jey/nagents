"""Fresh-process import boundaries cannot rely on already-loaded UI modules."""

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "mode",
    [
        "core",
        pytest.param("harness", marks=pytest.mark.requires_posix),
        pytest.param("custom", marks=pytest.mark.requires_posix),
        pytest.param("custom_subclass", marks=pytest.mark.requires_posix),
    ],
)
def test_execution_bridge_has_no_core_ui_import_dependency(tmp_path: "Path", mode: str) -> None:
    script = """
import asyncio
import importlib.abc
import sys
from pathlib import Path

mode = sys.argv[1]
blocked = ('nagents.web', 'nagents.tui', 'textual', 'fastapi', 'pydantic', 'uvicorn')
if mode == 'core':
    blocked += ('nagents.harness',)

class DenyOptionalImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('Unexpected optional import: ' + fullname)
        return None

sys.meta_path.insert(0, DenyOptionalImports())
from nagents import Agent
from nagents._delivery_execution import _DeliveryExecution
assert Agent._execution_bridge is None

if mode == 'harness':
    from nagents.harness import Harness, HarnessConfig
    async def run():
        workspace = Path(sys.argv[2])
        harness = Harness(HarnessConfig(workspace=workspace, data_dir=workspace / 'state', demo=True))
        try:
            events = [event async for event in harness.run('hello')]
            assert events
            assert harness.agent._execution_bridge is None
        finally:
            await harness.close()
    asyncio.run(run())

if mode in {'custom', 'custom_subclass'}:
    from nagents.events import TextDoneEvent, ToolCallEvent, ToolResultEvent
    from nagents.harness import Harness, HarnessConfig
    from nagents.harness.execution import bind_channel_send, delivery_origin
    from nagents.harness.runtime import _HarnessSession
    from nagents.session import SessionManager
    from tests.support.providers import FakeProvider

    async def run_custom():
        workspace = Path(sys.argv[2])
        harness = Harness(HarnessConfig(
            workspace=workspace, data_dir=workspace / 'state', auth='api-key', model='fake-model'
        ))
        calls = []

        async def script(provider, messages):
            if len(provider.requests) == 1:
                yield ToolCallEvent(id='send', name='channel_send', arguments={
                    'channel': 'external', 'destination': harness.session_id,
                })
            else:
                yield TextDoneEvent(text='done')

        async def approve(request):
            return True

        async def channel_send(channel: str, destination: str) -> str:
            assert harness.agent._execution_bridge is None
            try:
                delivery_origin(harness, channel, destination)
            except PermissionError:
                pass
            else:
                raise AssertionError('Custom adapter acquired local authority')
            calls.append(channel)
            return 'external delivery succeeded'

        class UnknownAdapter(_HarnessSession):
            pass

        adapter_type = SessionManager if mode == 'custom' else UnknownAdapter
        harness.agent.session = adapter_type(harness.agent.session.db_path)
        harness.agent.provider = FakeProvider(harness.config, 0, script)
        harness.agent.compactor = None
        harness.approval_handler = approve
        definition = harness.agent.register_tool(channel_send)
        try:
            with bind_channel_send(harness, definition):
                events = [event async for event in harness.run('send externally')]
            results = [event for event in events if isinstance(event, ToolResultEvent)]
            assert len(results) == 1
            assert results[0].error is None, results[0].error
            assert results[0].result == 'external delivery succeeded'
            assert calls == ['external']
        finally:
            await harness.close()
    asyncio.run(run_custom())

assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules for prefix in blocked)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, mode, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
