"""Independent runtime ownership preserves auth, history and retained handles."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from time import time
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.auth import OpenAIAuth
from nagents.harness.auth import _Tokens
from nagents.harness.runtime import Harness
from nagents.provider.openai import OpenAIProvider
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.web.test_web_subscription_lifecycle import until
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


def test_borrowed_chatgpt_auth_coalesces_refresh_and_clone_close_preserves_shared_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        closed: list[OpenAIAuth] = []

        async def close(auth: OpenAIAuth) -> None:
            closed.append(auth)

        monkeypatch.setattr(OpenAIAuth, "close", close)
        async with client_app(tmp_path) as (app, _, _, _):
            state = app.state.web
            parent = state.harness
            auth = parent.openai_auth
            clone = Harness(deepcopy(parent.config))
            await clone.agent.provider.close()
            clone.agent.provider = OpenAIProvider(clone.openai_auth.credentials, model="gpt-6-luna")
            clone.agent.session = state.history
            await state.executions.borrow_auth(clone)
            assert clone.openai_auth is auth and not clone._owns_auth
            assert clone.agent.provider._credentials == auth.credentials
            tokens = _Tokens("expired-fixture", "refresh-fixture", "identity-fixture", 0)
            refreshes = 0
            entered, release = asyncio.Event(), asyncio.Event()

            async def exchange(form: dict[str, str], deadline: float, previous: _Tokens | None = None) -> _Tokens:
                nonlocal refreshes
                refreshes += 1
                entered.set()
                await release.wait()
                return _Tokens("fresh-fixture", "refresh-fixture", "identity-fixture", time() + 3600)

            def save(value: _Tokens) -> None:
                nonlocal tokens
                tokens = value

            monkeypatch.setattr(auth, "_load", lambda: tokens)
            monkeypatch.setattr(auth, "_exchange", exchange)
            monkeypatch.setattr(auth, "_save", save)
            first = asyncio.create_task(auth.credentials())
            async with asyncio.timeout(HANG_GUARD):
                await entered.wait()
                second = asyncio.create_task(clone.agent.provider._credentials())
                release.set()
                await asyncio.gather(first, second)
            assert refreshes == 1
            await clone.close()
            assert auth not in closed
            assert await state.history.snapshot(state.selected_session_id) == []
        assert closed.count(auth) == 1

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_retained_clone_adopts_idle_settings_without_freezing_temporary_read_only_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        held, release = asyncio.Event(), asyncio.Event()
        (tmp_path / "read.txt").write_text("read snapshot A")
        observed: list[tuple[str, str]] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            prompt = str(messages[-1].content)
            if prompt == "A":
                yield ToolCallEvent(id="child-a", name="delegate", arguments={"prompt": "child A"})
                yield ToolCallEvent(id="read-a", name="read_file", arguments={"path": "read.txt"})
            elif messages[-1].role == "tool" and messages[-1].name == "read_file":
                held.set()
                await release.wait()
                yield TextDoneEvent(text="A held work complete")
            elif prompt == "B":
                yield ToolCallEvent(id="child-b", name="delegate", arguments={"prompt": "child B"})
            else:
                if prompt == "B again":
                    observed.append((state.running_harness.mode, provider.model))
                yield TextDoneEvent(text="finished")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            first = state.selected_session_id
            second = await state.harness.new_session()
            settings = (await client.get("/api/settings", headers=headers)).json()
            settings["values"]["read_only"] = True
            assert (
                await client.post(
                    "/api/settings",
                    headers=headers,
                    json={"revision": settings["revision"], "values": settings["values"]},
                )
            ).status_code == 200
            assert state.harness._permission_ceiling == "build"
            try:
                await state.queued_inputs.submit(first, "a", "A")
                async with asyncio.timeout(HANG_GUARD):
                    await held.wait()
                snapshots = dict(state.harness.tools.read_hashes)
                assert snapshots
                await state.queued_inputs.submit(second, "b", "B")
                await until(lambda: second in state.executions.owners and state.run_for(second) is None)
                clone = state.executions.owners[second]
                assert clone is not state.harness and clone.tasks._infos
                assert clone._permission_ceiling == "build" and clone.mode == "reviewer"
                assert clone.openai_auth is state.harness.openai_auth and not clone._owns_auth
                assert clone.tools.read_hashes is not state.harness.tools.read_hashes
                assert state.harness.tools.read_hashes == snapshots
                assert state.harness.tasks._infos
                release.set()
                await until(lambda: not state.executions.runs and not state.channels.work_tasks)
                settings = (await client.get("/api/settings", headers=headers)).json()
                settings["values"]["read_only"] = False
                settings["values"]["model"] = "changed-fixture-model"
                result = await client.post(
                    "/api/settings",
                    headers=headers,
                    json={"revision": settings["revision"], "values": settings["values"]},
                )
                assert result.status_code == 200, result.text
                await state.queued_inputs.submit(second, "b-again", "B again")
                await until(lambda: bool(observed) and state.run_for(second) is None)
                assert state.executions.owners[second] is clone and clone.tasks._infos
                assert observed == [("build", "changed-fixture-model")]
            finally:
                release.set()

    asyncio.run(scenario())
