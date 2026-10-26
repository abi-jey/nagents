"""Workspace tool settings wait for brief web bookkeeping mutations."""

from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager
from contextlib import asynccontextmanager
from contextlib import contextmanager
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import patch

from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.web.channels.host import ChannelHost
from nagents.web.routing import RoutingStore
from nagents.web.service import WebState
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Iterator
    from pathlib import Path

    import httpx
    import pytest


def config_for(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        tmp_path,
        data_dir=tmp_path / "data",
        providers=connection(auth="api-key"),
        agent="reviewer",
        max_subagent_depth=2,
        profiles={"reviewer": AgentProfile(mode="reviewer")},
    )


async def tool_catalog(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, object]:
    response = await client.get("/api/tools", headers=headers)
    assert response.status_code == 200
    return cast("dict[str, object]", response.json())


async def save_tools(
    client: httpx.AsyncClient, headers: dict[str, str], revision: str, agents: dict[str, dict[str, bool]]
) -> httpx.Response:
    return await client.post("/api/tools", headers=headers, json={"revision": revision, "agents": agents})


@contextmanager
def track_queued_mutations(entered: asyncio.Event, *, count: int = 1) -> Iterator[None]:
    original = WebState.queued_mutation
    arrivals = 0

    @asynccontextmanager
    async def tracked(self: WebState) -> AsyncIterator[None]:
        nonlocal arrivals
        arrivals += 1
        if arrivals == count:
            entered.set()
        async with original(self):
            yield

    with patch.object(WebState, "queued_mutation", tracked):
        yield


def test_tool_save_waits_for_inbox_claim_bookkeeping(tmp_path: Path) -> None:
    async def scenario() -> None:
        claim_entered = asyncio.Event()
        release_claim = asyncio.Event()
        pending_checks = 0

        async def has_pending(self: RoutingStore, **kwargs: object) -> bool:
            nonlocal pending_checks
            pending_checks += 1
            return pending_checks == 1

        async def gated_claim(self: ChannelHost) -> None:
            claim_entered.set()
            await release_claim.wait()
            return None

        with (
            patch.object(RoutingStore, "has_pending", has_pending),
            patch.object(ChannelHost, "claim", gated_claim),
        ):
            async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, _):
                try:
                    await asyncio.wait_for(claim_entered.wait(), HANG_GUARD)
                    catalog = await tool_catalog(client, headers)
                    revision = catalog["revision"]
                    assert isinstance(revision, str)
                    queued = asyncio.Event()
                    with track_queued_mutations(queued):
                        request = asyncio.create_task(
                            save_tools(client, headers, revision, {"reviewer": {"read_file": False}})
                        )
                        await asyncio.wait_for(queued.wait(), HANG_GUARD)
                        assert app.state.web.mutating
                        assert not request.done()
                        release_claim.set()
                        response = await asyncio.wait_for(request, HANG_GUARD)
                    assert response.status_code == 200, response.text
                    assert (tmp_path / ".ngn" / "tools.yaml").exists()
                finally:
                    release_claim.set()

    asyncio.run(scenario())


def test_concurrent_same_revision_saves_serialize_and_reject_stale_writer(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, _):
            catalog = await tool_catalog(client, headers)
            revision = catalog["revision"]
            assert isinstance(revision, str)
            owner: AbstractContextManager[None] = app.state.web.idle(allow_running=True)
            owner.__enter__()
            queued = asyncio.Event()
            with track_queued_mutations(queued, count=2):
                first = asyncio.create_task(save_tools(client, headers, revision, {"reviewer": {"read_file": False}}))
                second = asyncio.create_task(save_tools(client, headers, revision, {"reviewer": {"shell": False}}))
                try:
                    await asyncio.wait_for(queued.wait(), HANG_GUARD)
                    assert app.state.web.mutating
                    assert not first.done() and not second.done()
                finally:
                    owner.__exit__(None, None, None)
            responses = await asyncio.wait_for(asyncio.gather(first, second), HANG_GUARD)
            assert sorted(response.status_code for response in responses) == [200, 409]
            conflict = next(response for response in responses if response.status_code == 409)
            assert conflict.json() == {"detail": "Tool settings changed on disk. Refresh before saving."}

    asyncio.run(scenario())


def test_cancelled_queued_save_does_not_write_or_release_current_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, _):
            catalog = await tool_catalog(client, headers)
            revision = catalog["revision"]
            assert isinstance(revision, str)
            owner: AbstractContextManager[None] = app.state.web.idle(allow_running=True)
            owner.__enter__()
            queued = asyncio.Event()
            with track_queued_mutations(queued):
                request = asyncio.create_task(save_tools(client, headers, revision, {"reviewer": {"shell": False}}))
                try:
                    await asyncio.wait_for(queued.wait(), HANG_GUARD)
                    assert not request.done()
                    request.cancel()
                    with suppress(asyncio.CancelledError):
                        await request
                    assert app.state.web.mutating
                    assert not app.state.web.mutation_released.is_set()
                    assert not (tmp_path / ".ngn" / "tools.yaml").exists()
                finally:
                    owner.__exit__(None, None, None)

    asyncio.run(scenario())


def test_queued_save_times_out_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, _):
            catalog = await tool_catalog(client, headers)
            revision = catalog["revision"]
            assert isinstance(revision, str)
            owner: AbstractContextManager[None] = app.state.web.idle(allow_running=True)
            owner.__enter__()
            try:
                monkeypatch.setattr("nagents.web.service.MUTATION_WAIT_TIMEOUT", 0.01)
                queued = asyncio.Event()
                with track_queued_mutations(queued):
                    request = asyncio.create_task(save_tools(client, headers, revision, {"reviewer": {"shell": False}}))
                    await asyncio.wait_for(queued.wait(), HANG_GUARD)
                    response = await asyncio.wait_for(request, HANG_GUARD)
                assert response.status_code == 409
                assert response.json() == {"detail": "Workspace update is still in progress. Try again shortly."}
                assert not (tmp_path / ".ngn" / "tools.yaml").exists()
            finally:
                owner.__exit__(None, None, None)

    asyncio.run(scenario())


def test_queued_save_rejects_shutdown_without_writing(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, _):
            catalog = await tool_catalog(client, headers)
            revision = catalog["revision"]
            assert isinstance(revision, str)
            owner: AbstractContextManager[None] = app.state.web.idle(allow_running=True)
            owner.__enter__()
            app.state.web.executions.closed = True
            try:
                queued = asyncio.Event()
                with track_queued_mutations(queued):
                    request = asyncio.create_task(save_tools(client, headers, revision, {"reviewer": {"shell": False}}))
                    await asyncio.wait_for(queued.wait(), HANG_GUARD)
                    assert not request.done()
            finally:
                owner.__exit__(None, None, None)
            response = await asyncio.wait_for(request, HANG_GUARD)
            assert response.status_code == 503
            assert response.json() == {"detail": "The server is shutting down. Try again after it restarts."}
            assert not (tmp_path / ".ngn" / "tools.yaml").exists()

    asyncio.run(scenario())


def test_tool_approval_revoke_waits_for_mutation_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, _):
            called: list[tuple[str, str]] = []

            def revoke(tool: str, revision: str) -> bool:
                called.append((tool, revision))
                return True

            app.state.web.tool_approvals.revoke = revoke
            owner: AbstractContextManager[None] = app.state.web.idle(allow_running=True)
            owner.__enter__()
            queued = asyncio.Event()
            with track_queued_mutations(queued):
                request = asyncio.create_task(
                    client.post(
                        "/api/tools/approvals/revoke",
                        headers=headers,
                        json={"tool": "shell", "revision": "a" * 32},
                    )
                )
                try:
                    await asyncio.wait_for(queued.wait(), HANG_GUARD)
                    assert not request.done()
                    assert called == []
                finally:
                    owner.__exit__(None, None, None)
            response = await asyncio.wait_for(request, HANG_GUARD)
            assert response.status_code == 200, response.text
            assert called == [("shell", "a" * 32)]

    asyncio.run(scenario())
