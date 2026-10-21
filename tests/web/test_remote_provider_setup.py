"""A remote A2A agent does not require local model credentials."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.harness.config import AgentProfile
from nagents.harness.remote_profiles import RemoteProfile
from nagents.web.provider_setup import provider_setup
from tests.support.providers import FakeProvider
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.types import Message


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Remote agent credential storage requires POSIX")


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home, data_home = tmp_path / "config", tmp_path / "data"
    config_home.mkdir(mode=0o700)
    data_home.mkdir(mode=0o700)
    config_home.chmod(0o700)
    data_home.chmod(0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))


@pytest.mark.asyncio
async def test_remote_profile_is_ready_without_local_openai_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield TextDoneEvent(text="not used")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    try:
        store = harness._agent_catalog.remote_store
        store.save(
            {"peer": AgentProfile(description="Remote peer", a2a=RemoteProfile(url="https://peer.example"))},
            expected=store.load().revision,
        )
        assert harness.refresh_agent_definitions()
        await harness.set_agent("peer")

        assert provider_setup(harness) == {"configured": True, "message": ""}
    finally:
        await harness.close()
