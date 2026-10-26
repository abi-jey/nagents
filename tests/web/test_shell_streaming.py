"""Real shell output reaches the authenticated socket before the process exits."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import LiveStream
from tests.web.execution.test_web_wakeups import scheduled_app
from tests.web.test_web_tool_approvals import decide

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.asyncio
@pytest.mark.requires_posix
async def test_shell_stdout_and_stderr_stream_before_release_and_survive_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release-shell"
    process_id = tmp_path / "shell.pid"
    first = "stdout before release: café\nstderr before release\n"
    last = "stdout after release\nstderr after release\n"
    source = "\n".join(
        (
            "import os, sys, time",
            "from pathlib import Path",
            "Path(sys.argv[2]).write_text(str(os.getpid()))",
            "print('stdout before release: café', flush=True)",
            "print('stderr before release', file=sys.stderr, flush=True)",
            "while not Path(sys.argv[1]).exists():",
            "    time.sleep(0.01)",
            "print('stdout after release', flush=True)",
            "print('stderr after release', file=sys.stderr, flush=True)",
        )
    )
    command = shlex.join((sys.executable, "-u", "-c", source, str(release), str(process_id)))
    identity = {"generation_id": "shell-generation", "index": 0}
    continued = asyncio.Event()

    async def script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="shell-call", name="shell", arguments={"command": command}, extra=identity)
        else:
            continued.set()
            yield TextDoneEvent(text="The shell finished.")

    async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, providers, _):
        session_id = state.harness.session_id
        stream = LiveStream(app, headers, session_id, "Run the gated shell fixture")
        records: list[dict[str, object]] = []
        output = ""
        try:
            async with asyncio.timeout(HANG_GUARD):
                await stream.start_task
                while output != first:
                    record = await stream.output.get()
                    records.append(record)
                    assert record["event"] not in {"tool_result", "run_finished"}
                    if record["event"] == "approval":
                        assert record["tool"] == "shell"
                        assert (await decide(client, headers, record, "allow")).status_code == 200
                    elif record["event"] == "tool_output":
                        assert record["call_id"] == "shell-call" and record["extra"] == identity
                        output += str(record["text"])
                        assert first.startswith(output)

                # The child can only finish after this test creates its gate.
                # Receiving both streams now proves delivery during execution.
                assert not release.exists() and not continued.is_set()
                os.kill(int(process_id.read_text()), 0)
                assert len(providers[0].requests) == 1 and not stream.task.done()
                response = await client.get(f"/api/sessions/{session_id}", headers=headers)
                assert response.status_code == 200
                snapshot = cast("dict[str, object]", response.json())
                active = cast("dict[str, object]", snapshot["active_run"])
                for key in ("events", "records"):
                    saved = cast("list[dict[str, object]]", active[key])
                    assert "".join(str(item["text"]) for item in saved if item["event"] == "tool_output") == first
                    assert not any(item["event"] == "tool_result" for item in saved)

                release.touch()
                records.extend(await stream.run_to_completion())
                chunks = [record for record in records if record["event"] == "tool_output"]
                assert "".join(str(record["text"]) for record in chunks) == first + last
                assert all(record["call_id"] == "shell-call" and record["extra"] == identity for record in chunks)
                results = [record for record in records if record["event"] == "tool_result"]
                assert len(results) == 1
                assert results[0]["id"] == "shell-call" and results[0]["extra"] == identity
                assert results[0]["result"] == {
                    "output": first + last,
                    "exit_code": 0,
                    "timed_out": False,
                    "truncated": False,
                }
                assert records.index(chunks[-1]) < records.index(results[0])
                assert records[-1]["event"] == "run_finished" and records[-1]["status"] == "completed"
                assert continued.is_set() and len(providers[0].requests) == 2
        finally:
            release.touch()
            run = state.run_for(session_id)
            if run is not None:
                await asyncio.wait_for(state.stop(run), HANG_GUARD)
            if not stream.start_task.done():
                stream.start_task.cancel()
            await asyncio.gather(stream.start_task, return_exceptions=True)
            if not stream.socket_task.done():
                await stream.input.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(stream.socket_task, HANG_GUARD)
