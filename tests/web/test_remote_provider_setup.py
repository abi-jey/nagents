"""A remote A2A agent does not require local model credentials."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from nagents.events import ErrorEvent
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.harness.config import AgentProfile
from nagents.harness.remote_profiles import RemoteProfile
from nagents.web.provider_setup import provider_error
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


@pytest.mark.parametrize(
    ("code", "guidance"),
    [
        ("A2A_CONNECTION", "remote agent could not be reached or needs sign-in"),
        ("A2A_UNAVAILABLE", "Remote agents require nagents[a2a]"),
        ("A2A_HOST_SETUP", "has not advertised a public sign-in client"),
        ("A2A_PERMISSION", "read-only or offline demo mode"),
        ("A2A_BINDING_CHANGED", "different remote connection"),
        ("A2A_OUTCOME_UNKNOWN", "uncertain outcome"),
        ("A2A_TASK_FAILED", "did not complete this request"),
    ],
)
def test_known_a2a_errors_show_remote_guidance_without_provider_payload(code: str, guidance: str) -> None:
    event = ErrorEvent(
        message="PRIVATE_UPSTREAM_MESSAGE_MARKER",
        code=code,
        extra={
            "transport": {"reason": "PRIVATE_EXTRA_MARKER"},
            "provider": {"error": "PRIVATE_PROVIDER_MARKER"},
        },
    )

    category, message = provider_error(event)

    assert category == code.lower()
    assert guidance in message
    assert "PRIVATE_UPSTREAM_MESSAGE_MARKER" not in message
    assert "PRIVATE_EXTRA_MARKER" not in message
    assert "PRIVATE_PROVIDER_MARKER" not in message


def test_unknown_a2a_error_uses_generic_safe_fallback() -> None:
    event = ErrorEvent(
        message="PRIVATE_UPSTREAM_MESSAGE_MARKER",
        code="A2A_FUTURE_PRIVATE_CODE",
        extra={"details": "PRIVATE_EXTRA_MARKER"},
    )

    category, message = provider_error(event)

    assert category == "provider_failure"
    assert "Provider request failed." in message
    assert "Remote agent" not in message
    assert "A2A_FUTURE_PRIVATE_CODE" not in message
    assert "PRIVATE_UPSTREAM_MESSAGE_MARKER" not in message
    assert "PRIVATE_EXTRA_MARKER" not in message
