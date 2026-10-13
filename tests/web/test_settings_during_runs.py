"""Saved defaults can change without changing an admitted execution's runtime."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from nagents.channels.types import ChannelError
from nagents.events import TextDoneEvent
from nagents.types import Message
from tests.support.channels import site
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.support.web import submit_and_wait
from tests.web.test_web_subscription_lifecycle import until
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.web.live_runtime import LiveService
    from nagents.web.live_settings import LiveSettings
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


def test_connector_updates_conflict_only_with_that_connectors_own_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure("first")
        app.configure("second")
        host = app.state.channels
        assert app.client.portal is not None

        async def check() -> None:
            with host.using_channel("first"):
                with pytest.raises(HTTPException) as conflict:
                    host.require_replaceable("first")
                assert conflict.value.status_code == 409
                host.require_replaceable("second")
            with host.replacing_channel("first"):
                with pytest.raises(ChannelError, match="being updated"):
                    await host.channel_send("first", "destination", "Must not be sent")
                with host.using_channel("second"):
                    assert host._channel_calls == {"second": 1}
            assert not host._channel_calls and not host._replacing_channels

        app.client.portal.call(check)


@pytest.mark.parametrize("scope", ["workspace", "global"])
def test_saved_defaults_leave_execution_resources_alive_and_apply_to_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    from tests.support.providers import FakeProvider

    # This scripted provider supports the new preference for its fixture-only model IDs.
    monkeypatch.setattr(FakeProvider, "cached_reasoning_efforts", lambda self, model="": ("high",))

    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        observed: list[tuple[str, str]] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            prompt = str(messages[-1].content)
            observed.append((prompt, provider.model))
            if prompt == "first":
                entered.set()
                await release.wait()
                assert provider.model == "fake-model"
                assert not provider.closed
            yield TextDoneEvent(text=f"Finished {prompt}")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, providers, _):
            root = state.selected_session_id
            initial = state.harness
            auth = initial.openai_auth
            provider_close = AsyncMock(wraps=initial.agent.provider.close)
            resource_close = AsyncMock(wraps=initial.resources.aclose)
            auth_close = AsyncMock(wraps=auth.close)
            monkeypatch.setattr(initial.agent.provider, "close", provider_close)
            monkeypatch.setattr(initial.resources, "aclose", resource_close)
            monkeypatch.setattr(auth, "close", auth_close)
            result = await client.post(
                "/api/messages",
                headers=headers,
                json={"session_id": root, "message_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "prompt": "first"},
            )
            assert result.status_code == 200
            async with asyncio.timeout(HANG_GUARD):
                await entered.wait()
            active = state.run_for(root)
            assert active is not None and active.harness is initial
            work_task = state.channels.work_tasks[root]
            endpoint = "/api/settings" + ("/global" if scope == "global" else "")
            before = (await client.get(endpoint, headers=headers)).json()
            try:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json={
                        "revision": before["revision"],
                        "values": {
                            **before["values"],
                            "model": "next-model",
                            "max_output": 8192,
                            "reasoning_effort": "high",
                        },
                    },
                )
                assert response.status_code == 200, response.text
                primary = state.harness
                assert primary is not initial
                assert primary.config is not initial.config
                assert primary.config.model == "next-model"
                assert primary.reasoning_effort() == "high"
                assert initial.reasoning_effort() == ""
                assert initial.config.model == "fake-model"
                assert state.run_for(root) is active and not active.task.done()
                assert primary.openai_auth is initial.openai_auth is auth
                provider_close.assert_not_awaited()
                resource_close.assert_not_awaited()
                auth_close.assert_not_awaited()
                assert state.selected_session_id == root
                assert primary.agent.session is state.history
                assert primary.agent.tool_registry.get("channel_list") is not None
                assert initial.agent.tool_registry.get("channel_list") is not None
                # A second chat uses the latest defaults without waiting for this one.
                created = await client.post("/api/sessions/new", headers=headers, json={})
                assert created.status_code == 200
                second = created.json()["session_id"]
                await submit_and_wait(app, headers, second, "second")
                assert ("second", "next-model") in observed
                assert state.run_for(root) is active and not active.task.done()
                assert state.harness is primary
                release.set()
                # The inbox owner joins runtime disposal after the inner model
                # task and ownership-map removal have already finished.
                async with asyncio.timeout(HANG_GUARD):
                    await work_task
                provider_close.assert_awaited_once()
                resource_close.assert_awaited_once()
                auth_close.assert_not_awaited()
                assert any(row["content"] == "Finished first" for row in await state.history.snapshot(root))
                await submit_and_wait(app, headers, root, "third")
                assert ("third", "next-model") in observed
            finally:
                release.set()
        auth_close.assert_awaited_once()
        assert all(provider.closed for provider in providers)
        async with client_app(tmp_path, controlled=False) as (_, client, headers, _):
            saved = (await client.get("/api/settings", headers=headers)).json()
            assert saved["values"]["model"] == "next-model"
            assert saved["values"]["max_output"] == 8192
            assert saved["values"]["reasoning_effort"] == "high"

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["catalog", "summary"])
@pytest.mark.parametrize("cancel", [False, True])
def test_auxiliary_reads_keep_the_accepted_provider_during_settings_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, cancel: bool
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            assert messages[0].role == "system"
            entered.set()
            await release.wait()
            assert provider.model == "fake-model" and not provider.closed
            yield TextDoneEvent(text="The project is Juniper.")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            initial = state.harness
            provider = initial.agent.provider
            close = AsyncMock(wraps=provider.close)
            auth_close = AsyncMock(wraps=initial.openai_auth.close)
            monkeypatch.setattr(provider, "close", close)
            monkeypatch.setattr(initial.openai_auth, "close", auth_close)

            async def catalog() -> list[str]:
                entered.set()
                await release.wait()
                assert provider.model == "fake-model"
                close.assert_not_awaited()
                return ["accepted-model"]

            monkeypatch.setattr(provider, "get_model_list", catalog)
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="The project is Juniper."))
            saved_voice = (await client.get("/api/live/settings?scope=workspace", headers=headers)).json()
            saved_voice = (
                await client.post(
                    "/api/live/settings",
                    headers=headers,
                    json={
                        "scope": "workspace",
                        "revision": saved_voice["revision"],
                        "overrides": {"context_mode": "summary"},
                    },
                )
            ).json()

            async def read() -> str:
                if kind == "catalog":
                    result = await client.get("/api/models", headers=headers)
                    assert result.status_code == 200, result.text
                    return str(result.json()["models"][0])
                settings: LiveSettings = app.state.live_settings
                service: LiveService = app.state.live
                assert service._context_factory is not None
                # Exercise the real context callback with the admission's
                # captured settings, without holding HTTP provisioning's lock.
                async with settings.admit(saved_voice["revision"], root):
                    seed = await service._context_factory("test-voice-context")
                    return seed.method

            pending = asyncio.create_task(read())
            try:
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                before = (await client.get("/api/settings", headers=headers)).json()
                saved = await client.post(
                    "/api/settings",
                    headers=headers,
                    json={"revision": before["revision"], "values": {**before["values"], "model": "next-model"}},
                )
                assert saved.status_code == 200, saved.text
                assert state.harness is not initial
                assert state.harness.agent.provider.model == "next-model"
                assert initial.agent.provider is provider and provider.model == "fake-model"
                close.assert_not_awaited()
                auth_close.assert_not_awaited()
                if cancel:
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                else:
                    release.set()
                    assert await pending == ("accepted-model" if kind == "catalog" else "generated_summary")
                close.assert_awaited_once()
                auth_close.assert_not_awaited()
                assert not state.executions._leases and not state.executions._retired
                assert not state.executions._auxiliary
                assert (await state.history.snapshot(root))[0]["content"] == "The project is Juniper."
            finally:
                release.set()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        auth_close.assert_awaited_once()

    asyncio.run(scenario())


def test_retired_run_waits_for_the_last_auxiliary_reader_before_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered, release_run = asyncio.Event(), asyncio.Event()
        release_reads = (asyncio.Event(), asyncio.Event())
        reading = (asyncio.Event(), asyncio.Event())

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            entered.set()
            await release_run.wait()
            yield TextDoneEvent(text="Finished")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            initial = state.harness
            root = state.selected_session_id
            close = AsyncMock(wraps=initial.agent.provider.close)
            monkeypatch.setattr(initial.agent.provider, "close", close)
            response = await client.post(
                "/api/messages",
                headers=headers,
                json={"session_id": root, "message_id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "prompt": "Work"},
            )
            assert response.status_code == 200
            await entered.wait()

            async def read(index: int) -> None:
                async with state.executions.auxiliary_provider() as provider:
                    assert provider is initial.agent.provider
                    reading[index].set()
                    await release_reads[index].wait()
                    close.assert_not_awaited()

            readers = [asyncio.create_task(read(index)) for index in range(2)]
            try:
                await asyncio.gather(*(event.wait() for event in reading))
                before = (await client.get("/api/settings", headers=headers)).json()
                response = await client.post(
                    "/api/settings",
                    headers=headers,
                    json={"revision": before["revision"], "values": {**before["values"], "model": "next-model"}},
                )
                assert response.status_code == 200, response.text
                release_run.set()
                await until(lambda: root not in state.executions.owners)
                close.assert_not_awaited()
                release_reads[0].set()
                await readers[0]
                close.assert_not_awaited()
                release_reads[1].set()
                await readers[1]
                close.assert_awaited_once()
                assert not state.executions._retired and not state.executions._leases
            finally:
                release_run.set()
                for event in release_reads:
                    event.set()
                await asyncio.gather(*readers, return_exceptions=True)

    asyncio.run(scenario())


def test_provider_edit_and_activation_do_not_close_running_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            entered.set()
            await release.wait()
            assert not provider.closed
            yield TextDoneEvent(text="Finished with original provider")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            initial = state.harness
            root = state.selected_session_id
            response = await client.post(
                "/api/messages",
                headers=headers,
                json={"session_id": root, "message_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "prompt": "work"},
            )
            assert response.status_code == 200
            async with asyncio.timeout(HANG_GUARD):
                await entered.wait()
            provider = initial.agent.provider
            close = AsyncMock(wraps=provider.close)
            monkeypatch.setattr(provider, "close", close)
            work_task = state.channels.work_tasks[root]
            try:
                registry = (await client.get("/api/provider-scopes/global/providers", headers=headers)).json()
                changed = await client.put(
                    "/api/provider-scopes/global/providers/fixture",
                    headers=headers,
                    json={
                        "revision": registry["revision"],
                        "profile": {"kind": "openai", "auth": "api-key", "request_timeout": 90},
                    },
                )
                assert changed.status_code == 200, changed.text
                saved = await client.put(
                    "/api/provider-scopes/global/providers/second",
                    headers=headers,
                    json={"revision": changed.json()["revision"], "profile": {"kind": "anthropic", "auth": "api-key"}},
                )
                assert saved.status_code == 200, saved.text
                selected = await client.post(
                    "/api/provider-scopes/global/providers/second/activate",
                    headers=headers,
                    json={"revision": saved.json()["revision"]},
                )
                assert selected.status_code == 200, selected.text
                assert state.harness.config.provider == "second"
                assert initial.config.provider == "fixture"
                assert initial.agent.provider is provider
                close.assert_not_awaited()
                assert state.run_for(root) is not None
            finally:
                release.set()
            async with asyncio.timeout(HANG_GUARD):
                await work_task
            close.assert_awaited_once()

    asyncio.run(scenario())
