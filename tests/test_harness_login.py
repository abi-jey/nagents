"""Authentication routing and user controls, without live login or credentials."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from time import monotonic

import pytest
from aiohttp import web

from nagents.cli import main
from nagents.events import DoneEvent
from nagents.events import ToolResultEvent
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness import load_config
from nagents.harness import runtime
from nagents.harness.auth import DeviceAuthorization
from nagents.harness.provider import HarnessProvider
from nagents.provider import codex as codex_module
from nagents.provider.codex import DEFAULT_CODEX_MODEL
from nagents.provider.codex import CodexCredentials
from nagents.provider.codex import CodexProvider


class FakeAuth:
    def __init__(self, *, saved: bool = False, wait: bool = False) -> None:
        self.saved = saved
        self.wait = wait
        self.started = 0
        self.closed = False
        self.polling = asyncio.Event()
        self.cancelled = False

    def logged_in(self) -> bool:
        return self.saved

    def status(self) -> str:
        return "ChatGPT device login saved" if self.saved else "Not signed in"

    async def start_device_login(self) -> DeviceAuthorization:
        self.started += 1
        return DeviceAuthorization(
            verification_url="https://auth.openai.com/codex/device",
            user_code="TEST-CODE",
            device_auth_id="not-a-real-device-id",
            interval=1,
            expires_at=monotonic() + 900,
        )

    async def complete_device_login(self, authorization: DeviceAuthorization) -> None:
        self.polling.set()
        try:
            if self.wait:
                await asyncio.Event().wait()
            self.saved = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def credentials(self) -> CodexCredentials:
        return CodexCredentials(access_token="test-access-do-not-display", account_id="test-account")

    def logout(self) -> None:
        self.saved = False

    async def close(self) -> None:
        self.closed = True


def test_login_switches_protocol_without_polluting_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAuth()
    monkeypatch.setattr(runtime, "OpenAIAuth", lambda: fake)

    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path))
        shown: list[str] = []

        async def show(code: DeviceAuthorization) -> None:
            shown.append(code.user_code)
            assert not fake.saved

        try:
            await harness.login(show)
            assert shown == ["TEST-CODE"]
            assert fake.saved
            assert harness.config.auth == "chatgpt"
            assert harness.config.model == DEFAULT_CODEX_MODEL
            assert isinstance(harness.agent.provider, CodexProvider)
            assert await harness.history() == []
            assert "TEST-CODE" not in harness.describe()
            assert "test-access-do-not-display" not in harness.describe()
            await harness.logout()
            assert not fake.saved
            assert harness.config.auth == "api-key"
            assert harness.config.model == "gpt-4.1"
            assert isinstance(harness.agent.provider, HarnessProvider)
        finally:
            await harness.close()
        assert fake.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "auth,endpoint,expected", [("auto", "", True), ("api-key", "", False), ("auto", "https://example.test/v1", False)]
)
def test_saved_oauth_never_reaches_custom_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth: str, endpoint: str, expected: bool
) -> None:
    fake = FakeAuth(saved=True)
    monkeypatch.setattr(runtime, "OpenAIAuth", lambda: fake)

    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, auth=auth, base_url=endpoint))
        try:
            await harness.initialize()
            assert isinstance(harness.agent.provider, CodexProvider) is expected
            assert fake.started == 0
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider,endpoint,demo",
    [("openai", "https://example.test/v1", False), ("anthropic", "", False), ("openai", "", True)],
)
def test_login_rejects_incompatible_modes_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, endpoint: str, demo: bool
) -> None:
    fake = FakeAuth()
    monkeypatch.setattr(runtime, "OpenAIAuth", lambda: fake)

    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, provider=provider, base_url=endpoint, demo=demo))

        async def show(code: DeviceAuthorization) -> None:
            raise AssertionError("Must not request a device code")

        try:
            with pytest.raises(ValueError):
                await harness.login(show)
            assert fake.started == 0
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_cancel_login_awaits_polling_and_releases_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAuth(wait=True)
    monkeypatch.setattr(runtime, "OpenAIAuth", lambda: fake)

    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path))

        async def show(code: DeviceAuthorization) -> None:
            pass

        task = asyncio.create_task(harness.login(show))
        try:
            await asyncio.wait_for(fake.polling.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert fake.cancelled
            assert not fake.saved
            assert isinstance(harness.agent.provider, HarnessProvider)
            await harness.new_session()
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_cli_login_and_status_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeAuth()
    monkeypatch.setattr(runtime, "OpenAIAuth", lambda: fake)
    assert main(["login", "--workspace", str(tmp_path), "--device-auth"]) == 0
    output = capsys.readouterr().out
    assert "TEST-CODE" in output
    assert "test-access-do-not-display" not in output
    assert "not-a-real-device-id" not in output
    assert main(["login", "--workspace", str(tmp_path), "--status"]) == 0
    assert "saved" in capsys.readouterr().out
    assert fake.started == 1


def test_chatgpt_configuration_rejects_routing_overrides(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="without base_url"):
        HarnessConfig(workspace=tmp_path, auth="chatgpt", base_url="https://example.test/v1")
    with pytest.raises(ValueError, match="OpenAI provider"):
        HarnessConfig(workspace=tmp_path, auth="chatgpt", provider="anthropic")


def test_file_tools_cannot_read_oauth_store_even_with_custom_session_path(tmp_path: Path) -> None:
    harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "separate-sessions", demo=True))
    credential = Path(os.environ["XDG_DATA_HOME"]) / "ngn/auth/openai.json"
    with pytest.raises(PermissionError, match="OAuth credential"):
        harness.tools.relative(str(credential))
    with pytest.raises(PermissionError, match="Protected path"):
        harness.tools.relative(".codex/auth.json")


def test_initial_storage_is_private_under_group_writable_umask(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path))
        previous_umask = os.umask(0o002)
        try:
            await harness.initialize()
            assert stat.S_IMODE(harness.config.data_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE(Path(os.environ["XDG_DATA_HOME"]).stat().st_mode) == 0o700
            with harness.openai_auth._directory(create=True):
                assert stat.S_IMODE(harness.openai_auth.path.parent.stat().st_mode) == 0o700
        finally:
            os.umask(previous_umask)
            await harness.close()

    asyncio.run(scenario())


def test_empty_xdg_settings_cannot_trust_working_directory_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = home / "project"
    (project / "ngn").mkdir(parents=True)
    (project / "ngn/config.toml").write_text('model = "untrusted-model"\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("XDG_DATA_HOME", "")
    monkeypatch.chdir(project)
    config = load_config(project)
    assert config.model == "gpt-4.1"
    assert config.data_dir == home / ".local/share/ngn"
    harness = Harness(HarnessConfig(workspace=home, data_dir=tmp_path / "separate-state", demo=True))
    with pytest.raises(PermissionError, match="OAuth credential"):
        harness.tools.relative(str(home / ".local/share/ngn/auth/openai.json"))


def test_saved_login_drives_real_responses_tool_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAuth(saved=True)
    monkeypatch.setattr(runtime, "OpenAIAuth", lambda: fake)
    (tmp_path / "sample.txt").write_text("sample content\n")
    requests: list[object] = []

    async def responses(request: web.Request) -> web.Response:
        assert request.headers.get("Authorization") == "Bearer test-access-do-not-display"
        assert request.headers.get("ChatGPT-Account-Id") == "test-account"
        payload: object = await request.json()
        requests.append(payload)
        assert isinstance(payload, dict)
        assert payload.get("store") is False
        assert payload.get("stream") is True
        items = payload.get("input", [])
        assert isinstance(items, list)
        has_result = any(isinstance(item, dict) and item.get("type") == "function_call_output" for item in items)
        output: list[dict[str, object]]
        frames: list[dict[str, object]]
        if has_result:
            assert "sample content" in json.dumps(items)
            output = [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Read verified."}]}
            ]
            frames = [{"type": "response.output_text.delta", "delta": "Read verified."}]
        else:
            call: dict[str, object] = {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": '{"path":"sample.txt"}',
            }
            output = [call]
            frames = [
                {"type": "response.output_item.added", "output_index": 0, "item": {**call, "arguments": ""}},
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "output_index": 0,
                    "delta": '{"path":',
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "output_index": 0,
                    "delta": '"sample.txt"}',
                },
                {"type": "response.output_item.done", "output_index": 0, "item": call},
            ]
        frames.append(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": output,
                    "usage": {"input_tokens": 100, "output_tokens": 12, "total_tokens": 112},
                },
            }
        )
        return web.Response(
            text="".join(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n" for frame in frames),
            content_type="text/event-stream",
        )

    async def scenario() -> None:
        app = web.Application()
        app.router.add_post("/responses", responses)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        monkeypatch.setattr(codex_module, "CODEX_ENDPOINT", f"http://127.0.0.1:{runner.addresses[0][1]}/responses")
        harness = Harness(HarnessConfig(workspace=tmp_path))
        try:
            events = [event async for event in harness.run("Read sample.txt")]
            assert len(requests) == 2
            results = [event for event in events if isinstance(event, ToolResultEvent)]
            assert len(results) == 1
            assert not results[0].error
            assert any(isinstance(event, DoneEvent) and event.final_text == "Read verified." for event in events)
            assert "test-access-do-not-display" not in repr(await harness.history())
        finally:
            await harness.close()
            await runner.cleanup()

    asyncio.run(scenario())
