"""A remote A2A agent does not require local model credentials."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from nagents.events import ErrorEvent
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.harness.config import AgentProfile
from nagents.harness.remote.profiles import RemoteProfile
from nagents.web.settings.provider_setup import provider_error
from nagents.web.settings.provider_setup import provider_setup
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
        ("A2A_AUTH_REQUIRED", "sign-in is missing or expired"),
        ("A2A_AUTH_REFRESH_UNCERTAIN", "previous refresh token will not be reused"),
        ("A2A_AUTH_REJECTED", "rejected the access token"),
        ("A2A_AUTH_STORAGE", "credential storage permissions"),
        ("A2A_TOKEN_MISSING", "configured server token is missing"),
        ("A2A_FORBIDDEN", "denied access"),
        ("A2A_RATE_LIMIT", "rate limited"),
        ("A2A_SERVER_ERROR", "server returned an error"),
        ("A2A_CONNECTION", "Check its server address and network connection"),
        ("A2A_TIMEOUT", "request timed out"),
        ("A2A_TLS", "TLS connection could not be verified"),
        ("A2A_PROTOCOL", "invalid or unsupported response"),
        ("A2A_UNAVAILABLE", "Remote agents require nagents[a2a]"),
        ("A2A_HOST_SETUP", "has not advertised a public sign-in client"),
        ("A2A_PERMISSION", "read-only or demo restrictions"),
        ("A2A_BINDING_CHANGED", "different remote connection"),
        ("A2A_OUTCOME_UNKNOWN", "uncertain outcome"),
        ("A2A_TASK_FAILED", "reported that its task failed"),
        ("A2A_TASK_REJECTED", "rejected this task"),
        ("A2A_TASK_CANCELLED", "was cancelled"),
        ("A2A_RUNTIME", "local remote-agent runtime failed"),
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


@pytest.mark.parametrize(
    ("outcome", "guidance"),
    [
        ("not_sent", "No task was sent."),
        (
            "unknown",
            "The request may have reached the remote agent. It was not automatically retried; inspect its task",
        ),
        (
            "rejected",
            "The remote server did not accept a task. This request was not automatically retried.",
        ),
        ("known", ""),
    ],
)
def test_remote_error_explains_only_the_allowlisted_dispatch_outcome(outcome: str, guidance: str) -> None:
    event = ErrorEvent(
        message="PRIVATE_UPSTREAM_MESSAGE_MARKER",
        code="A2A_CONNECTION",
        extra={"remote_outcome": outcome, "exception": "PRIVATE_EXCEPTION_MARKER", "token": "PRIVATE_TOKEN_MARKER"},
    )

    category, message = provider_error(event)

    assert category == "a2a_connection"
    assert guidance in message
    for marker in ("PRIVATE_UPSTREAM_MESSAGE_MARKER", "PRIVATE_EXCEPTION_MARKER", "PRIVATE_TOKEN_MARKER"):
        assert marker not in message
