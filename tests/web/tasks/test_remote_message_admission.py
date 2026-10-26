"""Remote profiles may be admitted without a local provider connection."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.remote_profiles import RemoteProfile
from nagents.web.session_agents import Choice
from tests.support.web import LiveStream
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.requires_posix


def remote_config(tmp_path: Path, *, agent: str) -> HarnessConfig:
    return HarnessConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data",
        agent=agent,
        profiles={
            "remote-peer": AgentProfile(
                description="Remote peer",
                a2a=RemoteProfile(
                    url="https://peer.example.test",
                    token_env="NGN_REMOTE_ADMISSION_TOKEN",
                ),
            )
        },
    )


def test_session_selected_remote_agent_is_admitted_without_local_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.delenv("NGN_REMOTE_ADMISSION_TOKEN", raising=False)
        async with client_app(
            tmp_path,
            controlled=False,
            config=remote_config(tmp_path, agent="assistant"),
        ) as (app, client, headers, _):
            state = app.state.web
            root = state.selected_session_id
            selection = (await client.get(f"/api/sessions/{root}/agent", headers=headers)).json()
            selected = await client.post(
                f"/api/sessions/{root}/agent",
                headers=headers,
                json={"revision": selection["revision"], "agent": "remote-peer"},
            )
            assert selected.status_code == 200, selected.text
            assert selected.json()["effective"]["provider_kind"] == "a2a"
            assert state.harness.config.provider == ""

            stream = LiveStream(app, headers, root, "Ask the remote peer without a local provider.")
            events = await stream.run_to_completion()
            await stream.disconnect()
            errors = [event for event in events if event.get("event") == "error"]
            assert len(errors) == 1
            assert "configured server token is missing or invalid" in str(errors[0].get("message", ""))
            assert "Select a named provider connection" not in str(errors[0])

    asyncio.run(scenario())


def test_local_target_with_no_named_provider_still_rejects_default_remote_workspace(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with client_app(
            tmp_path,
            controlled=False,
            config=remote_config(tmp_path, agent="remote-peer"),
        ) as (app, client, headers, _):
            state = app.state.web
            root = state.selected_session_id
            selected = await client.get(f"/api/sessions/{root}/agent", headers=headers)
            switched = await client.post(
                f"/api/sessions/{root}/agent",
                headers=headers,
                json={"revision": selected.json()["revision"], "agent": "assistant"},
            )
            assert switched.status_code == 200, switched.text
            assert switched.json()["effective"]["provider_kind"] != "a2a"
            assert state.harness.config.provider == ""

            response = await client.post(
                "/api/messages",
                headers=headers,
                json={
                    "session_id": root,
                    "message_id": "4b829d9e-93e5-432d-a8d3-518f62c8b9d3",
                    "prompt": "This local profile has no named provider.",
                },
            )
            assert response.status_code == 409
            assert "Select a named provider connection" in response.json()["detail"]
            assert not state.executions.runs

    asyncio.run(scenario())


def test_missing_selected_profile_cannot_use_remote_admission_exception(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(
            tmp_path,
            controlled=False,
            config=remote_config(tmp_path, agent="remote-peer"),
        ) as (app, client, headers, _):
            state = app.state.web
            root = state.selected_session_id
            state.session_agents.choices[root] = Choice("removed-peer", "0" * 64)
            response = await client.post(
                "/api/messages",
                headers=headers,
                json={
                    "session_id": root,
                    "message_id": "18b9dfb2-9024-4b2c-b653-2c9a5d0a5c99",
                    "prompt": "A removed remote profile must fail closed.",
                },
            )
            assert response.status_code == 409
            assert "Select a named provider connection" in response.json()["detail"]
            assert not state.executions.runs

    asyncio.run(scenario())
