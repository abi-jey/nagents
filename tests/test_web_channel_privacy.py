"""Only synthetic credentials; checks cover fixed channel output boundaries."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from importlib import metadata
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.channels.types import Channel
from nagents.channels.types import ChannelAction
from nagents.channels.types import ChannelDelivery
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelPlugin
from nagents.channels.types import ChannelSend
from nagents.channels.types import ChannelValue
from nagents.web.catalog import Connection
from nagents.web.channel_privacy import CredentialGuard
from nagents.web.channel_privacy import CredentialProtectionError
from tests.test_web_channels import FakeChannel
from tests.test_web_channels import site

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.types import JsonSchema
    from tests.test_web_channels import Site

pytestmark = pytest.mark.requires_posix
TOKEN = "synthetic-host-credential-alpha"
ROTATED = "synthetic-host-credential-beta"
NORMAL: dict[str, ChannelValue] = {"ok": True, "stable_identifier_ab": "table ngn-ab", "count": 2}


class EchoChannel(FakeChannel):
    def __init__(self, name: str, credential: str, mode: str) -> None:
        super().__init__(name)
        self.credential = credential
        self.mode = mode
        self.opened = 0
        self.actions_called = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.description = f"Bearer {credential}" if mode == "description" else "Synthetic connector"
        parameters: JsonSchema = {"type": "object", "properties": {}, "additionalProperties": False}
        if mode == "action-schema":
            parameters["properties"] = {"note": {"type": "string", "description": f"Credential: {credential}"}}
        self.actions = (ChannelAction("inspect", "Inspect payload", parameters),)

    async def open(self) -> None:
        self.opened += 1
        if self.mode == "open-description":
            self.description = self.credential

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        self.deliveries.append(message)
        if self.mode == "delayed-send":
            self.started.set()
            await self.release.wait()
        if self.mode in {"send-result", "delayed-send"}:
            return ChannelDelivery(("synthetic-delivery",), {"debug": {"authorization": f"Bearer {self.credential}"}})
        if self.mode == "send-key":
            return ChannelDelivery(("synthetic-delivery",), {self.credential: "private key echo"})
        if self.mode == "send-id":
            return ChannelDelivery((self.credential,))
        if self.mode == "error":
            raise ChannelError(self.credential, retry_after=7, outcome_unknown=False)
        return ChannelDelivery(("synthetic-delivery",), deepcopy(NORMAL))

    async def action(self, name: str, arguments: dict[str, ChannelValue]) -> dict[str, ChannelValue]:
        self.actions_called += 1
        if self.mode == "action-result":
            return {"result": [f"token={self.credential}"]}
        if self.mode == "error":
            raise ChannelError(self.credential, retry_after=9, outcome_unknown=True)
        return deepcopy(NORMAL)


def install(app: Site, monkeypatch: pytest.MonkeyPatch, mode: str) -> list[EchoChannel]:
    created: list[EchoChannel] = []

    def factory(config: dict[str, ChannelValue]) -> Channel:
        credential = config.get("token", "")
        assert isinstance(credential, str)
        channel = EchoChannel(str(config["name"]), credential, mode)
        created.append(channel)
        return channel

    descriptor = ChannelPlugin(
        "Fixture",
        "Synthetic transport",
        {
            "type": "object",
            "properties": {"label": {"type": "string"}, "token": {"type": "string", "writeOnly": True}},
            "additionalProperties": False,
        },
        factory,
    )
    monkeypatch.setattr(metadata.EntryPoint, "load", lambda self: descriptor)
    return created


@pytest.mark.parametrize("enabled", [True, False])
def test_duplicate_private_value_in_public_config_rejected_before_factory_or_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    with site(tmp_path, monkeypatch) as app:
        created = install(app, monkeypatch, "normal")
        before = app.client.get("/api/channels", headers=app.headers).json()
        response = app.client.put(
            "/api/channels/fixture",
            headers=app.headers,
            json={
                "revision": before["revision"],
                "plugin": "fixture",
                "enabled": enabled,
                "config": {"label": f"duplicate: {TOKEN}"},
                "secrets": {"token": TOKEN},
            },
        )
        assert response.status_code == 422 and TOKEN not in response.text
        assert not created and not app.state.channels.catalog.connections
        assert app.state.channels.catalog.revision == before["revision"]
        assert not app.state.channels.catalog.path.exists()


@pytest.mark.parametrize("mode", ["description", "action-schema", "open-description"])
def test_factory_or_open_catalog_echo_never_reaches_instructions_or_public_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        created = install(app, monkeypatch, mode)
        before = app.client.get("/api/channels", headers=app.headers).json()
        response = app.client.put(
            "/api/channels/fixture",
            headers=app.headers,
            json={
                "revision": before["revision"],
                "plugin": "fixture",
                "enabled": True,
                "config": {"label": "public"},
                "secrets": {"token": TOKEN},
            },
        )
        assert len(created) == 1 and created[0].credential == TOKEN
        if mode == "open-description":
            assert response.status_code == 200 and response.json()["connections"][0]["status"] == "error"
            assert created[0].opened == 1 and created[0].closed
        else:
            assert response.status_code == 422 and created[0].opened == 0
        assert TOKEN not in response.text + app.state.channels.instructions.text + caplog.text
        assert not app.state.channels.channels
        app.submit("ordinary request")
        app.idle()
        assert TOKEN not in repr(app.providers[0].requests)
        assert TOKEN not in json.dumps(app.history(app.main))


@pytest.mark.parametrize(
    "location", ["public-config", "descriptor-name", "descriptor-description", "descriptor-schema"]
)
def test_snapshot_rejects_unsafe_legacy_or_cached_public_data_without_rewriting_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        install(app, monkeypatch, "normal")
        app.configure(secrets={"token": TOKEN})
        catalog = app.state.channels.catalog
        plugin = catalog.plugins["fixture"]
        if location == "public-config":
            catalog.connections["fixture"].config["label"] = TOKEN
        elif location == "descriptor-name":
            plugin.name = TOKEN
        elif location == "descriptor-description":
            plugin.description = f"Uses {TOKEN}"
        else:
            plugin.schema = {"type": "object", "properties": {"label": {"type": "string", "enum": [TOKEN]}}}
        schema = deepcopy(plugin.schema)
        response = app.client.get("/api/channels", headers=app.headers)
        assert response.status_code == 422 and TOKEN not in response.text
        assert plugin.schema == schema  # Never alter schema enums, properties or identifiers to hide a value.


@pytest.mark.parametrize("mode", ["send-result", "send-key", "send-id", "action-result"])
def test_success_echo_is_withheld_with_post_dispatch_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        created = install(app, monkeypatch, mode)
        app.configure(secrets={"token": TOKEN})
        assert app.client.portal is not None
        with pytest.raises(ChannelError) as error:
            if mode == "action-result":
                app.client.portal.call(app.state.channels.channel_action, "fixture", "inspect", {})
            else:
                app.client.portal.call(app.state.channels.channel_send, "fixture", "synthetic-chat", "hello")
        details = json.loads(str(error.value))
        assert details["outcome_unknown"] is True and details["retry_after"] == 0
        assert error.value.outcome_unknown is True and TOKEN not in str(error.value)
        assert created[0].actions_called + len(created[0].deliveries) == 1


def test_unsafe_success_never_enters_model_tool_history_or_ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        created = install(app, monkeypatch, "send-result")
        app.configure(secrets={"token": TOKEN})
        observed: list[dict[str, object]] = []
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            observed.append(socket.receive_json())
            app.submit("privilege")
            while True:
                frame = socket.receive_json()
                observed.append(frame)
                if frame.get("record", {}).get("event") == "approval":
                    break
            approval = frame["record"]
            response = app.client.post(
                "/api/approval",
                headers=app.headers,
                json={
                    "run_id": approval["run_id"],
                    "approval_id": approval["approval_id"],
                    "call_id": approval["id"],
                    "decision": "allow",
                },
            )
            assert response.status_code == 200
            while True:
                frame = socket.receive_json()
                observed.append(frame)
                if frame.get("record", {}).get("event") == "run_finished":
                    break
        app.idle()
        assert len(created[0].deliveries) == 1
        assert TOKEN not in json.dumps(observed) + json.dumps(app.history(app.main)) + repr(app.providers[0].requests)
        tool = next(
            cast("dict[str, object]", frame["record"])
            for frame in observed
            if isinstance(frame.get("record"), dict)
            and cast("dict[str, object]", frame["record"]).get("event") == "tool_result"
        )
        assert json.loads(str(tool["error"]))["outcome_unknown"] is True


def test_retired_credentials_remain_protected_after_rotation_disable_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        created = install(app, monkeypatch, "delayed-send")
        app.configure(secrets={"token": TOKEN})
        assert app.client.portal is not None
        pending = app.client.portal.start_task_soon(
            app.state.channels.channel_send, "fixture", "synthetic-chat", "hello"
        )
        try:
            app.client.portal.call(asyncio.wait_for, created[0].started.wait(), 5)
            app.configure(enabled=False, secrets={"token": ROTATED})
            deleted = app.client.request(
                "DELETE",
                "/api/channels/fixture",
                headers=app.headers,
                json={"revision": app.state.channels.catalog.revision},
            )
            assert deleted.status_code == 200
            app.client.portal.call(created[0].release.set)
            with pytest.raises(ChannelError) as error:
                pending.result(timeout=5)
            assert TOKEN not in str(error.value) and error.value.outcome_unknown is True
            for credential in (TOKEN, ROTATED):
                with pytest.raises(CredentialProtectionError):
                    app.state.channels.catalog.protection.check({"echo": credential})
        finally:
            app.client.portal.call(created[0].release.set)


def test_disabled_credentials_loaded_on_restart_protect_public_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(enabled=False, secrets={"token": TOKEN})
    with site(tmp_path, monkeypatch) as app:
        assert not app.channels
        app.state.channels.catalog.connections["fixture"].config["label"] = TOKEN
        response = app.client.get("/api/channels", headers=app.headers)
        assert response.status_code == 422 and TOKEN not in response.text


def test_recheck_blocks_cached_catalog_before_model_injection_and_list_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        created = install(app, monkeypatch, "normal")
        app.configure(secrets={"token": TOKEN})
        created[0].description = ROTATED  # Initially public, before this value is declared private.
        app.state.channels.update_tools()
        catalog = app.state.channels.catalog
        catalog.save(
            {
                **catalog.connections,
                "disabled": Connection(
                    plugin="fixture",
                    enabled=False,
                    config={},
                    secrets={"token": ROTATED},
                    main_session_id=app.main,
                ),
            }
        )
        assert app.client.portal is not None
        with pytest.raises(ChannelError) as error:
            app.client.portal.call(app.state.channels.channel_list)
        details = json.loads(str(error.value))
        assert details["outcome_unknown"] is False and details["retry_after"] == 0 and ROTATED not in str(error.value)
        app.submit("ordinary request")
        app.idle()
        assert ROTATED not in repr(app.providers[0].requests) + app.state.channels.instructions.text


def test_normal_results_identifiers_and_unrelated_user_text_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        install(app, monkeypatch, "normal")
        assert app.client.portal is not None
        app.client.portal.call(app.state.harness.create_session, "ngn-ab")
        snapshot = app.configure(config={"label": "table"}, secrets={"token": "ab"}, main_session_id="ngn-ab")
        assert cast("list[dict[str, object]]", snapshot["connections"])[0]["main_session_id"] == "ngn-ab"
        result = app.client.portal.call(app.state.channels.channel_send, "fixture", "synthetic-chat", "hello")
        assert result == {"message_ids": ["synthetic-delivery"], "metadata": NORMAL}
        assert app.client.portal.call(app.state.channels.channel_action, "fixture", "inspect", {}) == NORMAL
        text = "ab is an unrelated identifier in my own message"
        app.submit(text)
        app.idle()
        history = cast("list[dict[str, object]]", app.history(app.main)["history"])
        assert history[0]["content"] == text
        assert any(message.content == text for message in app.providers[0].requests[0])


def test_existing_connector_error_flags_are_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        install(app, monkeypatch, "error")
        app.configure(secrets={"token": TOKEN})
        assert app.client.portal is not None
        with pytest.raises(ChannelError) as send_error:
            app.client.portal.call(app.state.channels.channel_send, "fixture", "synthetic-chat", "hello")
        with pytest.raises(ChannelError) as action_error:
            app.client.portal.call(app.state.channels.channel_action, "fixture", "inspect", {})
        assert send_error.value.retry_after == 7 and send_error.value.outcome_unknown is False
        assert action_error.value.retry_after == 9 and action_error.value.outcome_unknown is True
        assert TOKEN not in str(send_error.value) + str(action_error.value)


def test_credential_policy_is_bounded_and_does_not_forget_previous_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nagents.web.channel_privacy.MAX_CREDENTIALS", 2)
    guard = CredentialGuard()
    guard.remember({"first": TOKEN, "second": ROTATED})
    with pytest.raises(CredentialProtectionError) as error:
        guard.remember({"third": "synthetic-third-credential"})
    assert TOKEN not in str(error.value) and ROTATED not in str(error.value)
    for credential in (TOKEN, ROTATED):
        with pytest.raises(CredentialProtectionError):
            guard.check({"value": credential})
    guard.check({"stable_identifier": "normal unrelated output", "enabled": False})
