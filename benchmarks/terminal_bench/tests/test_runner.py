from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from benchmarks.terminal_bench.runner import AccessOnlyAuth
from benchmarks.terminal_bench.runner import EventLog
from benchmarks.terminal_bench.runner import configuration
from benchmarks.terminal_bench.runner import container_gate
from benchmarks.terminal_bench.runner import load_credentials
from benchmarks.terminal_bench.summarize import summarize_events
from tests.support.providers import setup_harness

from nagents.cli import _event_record
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness import Harness
from nagents.provider import OpenAIProvider
from nagents.provider.openai import CodexCredentials

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.support.providers import FakeProvider

    from nagents.events import Event
    from nagents.types import Message


def test_approval_runner_refuses_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NGN_BENCHMARK_ISOLATED", raising=False)
    with pytest.raises(RuntimeError, match="isolated Harbor"):
        container_gate(tmp_path / "absent")


@pytest.mark.requires_posix
def test_credentials_reject_refresh_tokens_and_public_permissions(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    payload = {"access_token": "private-token", "account_id": "account", "residency": ""}
    path.write_text(json.dumps(payload))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular"):
        load_credentials(path)
    path.chmod(0o600)
    assert load_credentials(path).access_token == "private-token"
    path.write_text(json.dumps({**payload, "refresh_token": "must-not-be-copied"}))
    with pytest.raises(ValueError, match="only access_token"):
        load_credentials(path)


@pytest.mark.requires_posix
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-luna"])
def test_access_only_auth_is_inherited_by_native_children(tmp_path: Path, model: str) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path, model, tmp_path / "private")
        harness = Harness(config)
        credentials = CodexCredentials("ephemeral-access", "account")
        harness.openai_auth = AccessOnlyAuth(credentials, tmp_path / "credentials.json")
        child: Harness | None = None
        try:
            await harness.initialize()
            child = harness.tasks._create_child("assistant")
            await child.initialize()
            assert isinstance(harness.agent.provider, OpenAIProvider)
            assert isinstance(child.agent.provider, OpenAIProvider)
            assert child.openai_auth is harness.openai_auth
            assert await child.openai_auth.credentials() is credentials
            assert harness.config.model == child.config.model == model
            private = Path(os.environ["XDG_CONFIG_HOME"])
            assert harness.provider_store.global_store.path.is_relative_to(private)
            assert harness.provider_store.workspace_store.path.is_relative_to(private)
            assert child.provider_store.global_store.path == harness.provider_store.global_store.path
            assert not harness.provider_store.load().providers
        finally:
            if child is not None:
                await child.close()
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_real_harness_loop_records_native_validation_and_shell_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="invalid", name="read_file", arguments={"path": "file.txt", "bogus": True})
            yield ToolCallEvent(id="nonzero", name="shell", arguments={"command": "exit 7"})
            yield ToolCallEvent(id="timeout", name="shell", arguments={"command": "sleep 5", "timeout": 0.01})
        else:
            yield TextDoneEvent(text="Observed native results")

    async def scenario() -> None:
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        log = EventLog(tmp_path / "events.jsonl")
        harness.approval_handler = log.approve
        try:
            async for event in harness.run("exercise the native harness"):
                log.write(_event_record(event))
            log.write({"event": "benchmark_end", "status": "completed"})
        finally:
            await harness.close()
        assert len(providers[0].requests) == 2

    asyncio.run(scenario())
    summary = summarize_events(tmp_path / "events.jsonl")
    counts = summary["counts"]
    assert isinstance(counts, dict)
    assert counts["tool_call"] == 3
    assert counts["tool_result"] == 3
    assert counts["tool_errors"] == 1
    assert counts["validation_error_hints"] == 1
    assert counts["approval_allow"] == 2
    assert counts["shell_nonzero_exits"] == 2
    assert counts["shell_timeouts"] == 1
    assert summary["completion_is_verifier_success"] is False


def test_event_redaction_and_interrupted_trial_are_not_success(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path, ("secret-token",))
    log.write({"event": "tool_result", "name": "read_file", "error": "Cannot read secret-token"})
    assert "secret-token" not in path.read_text()
    assert "[REDACTED]" in path.read_text()
    assert summarize_events(path)["status"] == "interrupted_without_end_event"


def test_fresh_trial_configuration_excludes_ambient_provider_registries(tmp_path: Path) -> None:
    ambient = Path(os.environ["XDG_CONFIG_HOME"]) / "ngn/providers.yaml"
    ambient.parent.mkdir(parents=True)
    ambient.write_text("not valid provider configuration")
    first = configuration(tmp_path, "gpt-6-astra", tmp_path / "private")
    first_config = Path(os.environ["XDG_CONFIG_HOME"])
    (first_config / "ngn").mkdir()
    (first_config / "ngn/providers.yaml").write_text("a previous trial must not supply providers")
    second = configuration(tmp_path, "gpt-6-luna", tmp_path / "private")
    second_config = Path(os.environ["XDG_CONFIG_HOME"])
    assert second_config != first_config and not (second_config / "ngn/providers.yaml").exists()
    assert first.data_dir != second.data_dir
    assert (second.provider, second.provider_id, second.auth, second.model) == ("openai", "", "chatgpt", "gpt-6-luna")
    assert ambient.read_text() == "not valid provider configuration"
