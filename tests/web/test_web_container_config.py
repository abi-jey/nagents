"""Container startup, config sources and credential onboarding without upstream calls."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
import yaml

from nagents.cli import main
from nagents.events import ErrorEvent
from nagents.harness.auth import OpenAIAuth
from nagents.harness.auth import OpenAIAuthError
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.web.provider_setup import provider_error
from tests.support.web import client_app


def test_container_serve_starts_from_defaults_or_env_without_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    with patch("nagents.web.serve") as serve:
        assert main(["serve", "--workspace", str(tmp_path)]) == 0
        config = serve.call_args.args[0]
        assert config.config_paths == () and config.auth == "auto" and not config.demo
        assert serve.call_args.kwargs["host"] == "127.0.0.1"  # The image opts into 0.0.0.0 explicitly.

        monkeypatch.setenv("NGN_PROVIDER", "anthropic")
        monkeypatch.setenv("NGN_MODEL", "claude-example")
        monkeypatch.setenv("NGN_AUTH", "api-key")
        monkeypatch.setenv("NGN_API_KEY_ENV", "TEST_CONTAINER_KEY")
        assert main(["serve", "--workspace", str(tmp_path)]) == 0
        configured = serve.call_args.args[0]
        assert (configured.provider, configured.model, configured.auth, configured.api_key_env) == (
            "anthropic",
            "claude-example",
            "api-key",
            "TEST_CONTAINER_KEY",
        )
        assert configured.config_paths == ()


def test_kubernetes_projected_file_is_explicit_trusted_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user"))
    example = Path(__file__).resolve().parents[2] / "examples/k8s/ngn-container-config.yaml"
    documents = list(yaml.safe_load_all(example.read_text()))
    assert [document["kind"] for document in documents] == ["ConfigMap", "Deployment", "Service"]
    content = documents[0]["data"]["config.yaml"]
    projection = tmp_path / "..2026_09_27_18_46_12"
    projection.mkdir()
    (projection / "config.yaml").write_text(content)
    (tmp_path / "..data").symlink_to(projection.name)
    (tmp_path / "config.yaml").symlink_to("..data/config.yaml")
    config = load_config(tmp_path, tmp_path / "config.yaml")
    assert config.config_paths == (projection / "config.yaml",)
    assert config.diagnostics == (f"Loaded trusted configuration: {projection / 'config.yaml'}",)
    assert (config.provider, config.model, config.auth, config.api_key_env) == (
        "openai",
        "gpt-4.1",
        "api-key",
        "OPENAI_API_KEY",
    )
    with patch("nagents.web.serve") as serve:
        assert main(["serve", "--workspace", str(tmp_path), "--config", str(tmp_path / "config.yaml")]) == 0
        assert serve.call_args.args[0].config_paths == config.config_paths


@pytest.mark.requires_posix
@pytest.mark.parametrize("key_name", ["OPENAI_API_KEY", "TEST_CONTAINER_MISSING_KEY"])
def test_web_starts_without_key_and_explains_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_name: str
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv(key_name, raising=False)

    async def check() -> None:
        config = (
            load_config(tmp_path)
            if key_name == "OPENAI_API_KEY"
            else HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "sessions", auth="api-key", api_key_env=key_name)
        )
        async with client_app(tmp_path, config=config, controlled=False) as (_, client, headers, _):
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert bootstrap["provider_setup"]["configured"] is False
            assert key_name in bootstrap["provider_setup"]["message"]
            session = (await client.get("/api/sessions", headers=headers)).json()["session_id"]
            response = await client.post("/api/run", json={"session_id": session, "prompt": "hello"}, headers=headers)
            events = [json.loads(line) for line in response.text.splitlines()]
            assert events[-1]["status"] == "failed"
            assert any(f"Set ${key_name}" in event.get("message", "") for event in events)
            assert all(event.get("event") not in {"tool_call", "text_chunk"} for event in events)

    asyncio.run(check())


@pytest.mark.requires_posix
def test_env_key_marks_setup_configured_without_exposing_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NGN_PROVIDER", "openai")
    monkeypatch.setenv("NGN_MODEL", "gpt-4.1")
    monkeypatch.setenv("NGN_AUTH", "api-key")
    monkeypatch.setenv("NGN_API_KEY_ENV", "TEST_CONTAINER_KEY")
    monkeypatch.setenv("TEST_CONTAINER_KEY", "PRIVATE-CONTAINER-KEY")

    async def check() -> None:
        config = load_config(tmp_path)
        async with client_app(tmp_path, config=config, controlled=False) as (_, client, headers, _):
            bootstrap = await client.get("/api/bootstrap")
            assert bootstrap.json()["provider_setup"] == {"configured": True, "message": ""}
            settings = await client.get("/api/settings", headers=headers)
            assert settings.json()["connection"]["auth"] == "api-key"
            assert settings.json()["values"]["model"] == "gpt-4.1"
            assert "PRIVATE-CONTAINER-KEY" not in bootstrap.text + settings.text

    asyncio.run(check())


@pytest.mark.requires_posix
def test_chatgpt_missing_login_or_unusable_credential_is_actionable_and_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE-API-KEY-IS-NOT-CODEX-AUTH")

    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "sessions", auth="chatgpt")
        async with client_app(tmp_path, config=config, controlled=False) as (_, client, headers, _):
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert bootstrap["provider_setup"]["configured"] is False
            session = (await client.get("/api/sessions", headers=headers)).json()["session_id"]
            response = await client.post("/api/run", json={"session_id": session, "prompt": "hello"}, headers=headers)
            assert "ngn login chatgpt" in response.text and '"status": "failed"' in response.text
            assert "PRIVATE-API-KEY-IS-NOT-CODEX-AUTH" not in response.text

        with (
            patch.object(OpenAIAuth, "logged_in", return_value=True),
            patch.object(OpenAIAuth, "credentials", AsyncMock(side_effect=OpenAIAuthError("PRIVATE-UPSTREAM"))),
        ):
            async with client_app(tmp_path, config=config, controlled=False) as (_, client, headers, _):
                assert (await client.get("/api/bootstrap")).json()["provider_setup"]["configured"] is True
                session = (await client.get("/api/sessions", headers=headers)).json()["session_id"]
                response = await client.post(
                    "/api/run", json={"session_id": session, "prompt": "hello"}, headers=headers
                )
                assert "login is unavailable, expired, or rejected" in response.text
                assert '"status": "failed"' in response.text
                assert "PRIVATE-UPSTREAM" not in response.text
                assert "PRIVATE-UPSTREAM" not in caplog.text
                assert "reason=chatgpt_auth" in caplog.text

    asyncio.run(check())


def test_web_provider_errors_use_only_known_codes() -> None:
    unknown = ErrorEvent(message="PRIVATE-UPSTREAM", code="PRIVATE-UPSTREAM")
    category, text = provider_error(unknown)
    assert category == "provider_failure" and "PRIVATE-UPSTREAM" not in text
    for code, reason in (
        ("CODEX_HTTP_401", "chatgpt_auth"),
        ("CODEX_HTTP_403", "chatgpt_access"),
        ("CODEX_HTTP_429", "chatgpt_limit"),
        ("CODEX_HTTP_404", "chatgpt_model"),
        ("CODEX_CONNECTION", "chatgpt_network"),
        ("401", "api_auth"),
    ):
        category, text = provider_error(ErrorEvent(message="PRIVATE-UPSTREAM", code=code))
        assert category == reason and "PRIVATE-UPSTREAM" not in text
