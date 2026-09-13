"""Demo must never execute installed channel descriptors, factories, or transports."""

from __future__ import annotations

import asyncio
import sys
import uuid
from importlib import metadata
from types import ModuleType
from typing import TYPE_CHECKING
from unittest.mock import Mock

import aiosqlite
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from nagents.channels.runtime import _envelope
from nagents.channels.types import ChannelCommand
from nagents.channels.types import ChannelMessage
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.provider import Provider
from nagents.web.app import create_app
from nagents.web.catalog import ChannelCatalog
from nagents.web.catalog import Connection
from nagents.web.catalog import SavedCatalog
from nagents.web.routing import RoutingStore

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.web.service import WebState

URL = "http://127.0.0.1:8765"
pytestmark = pytest.mark.requires_posix


def fingerprint(workspace: Path) -> dict[str, tuple[bool, bytes]]:
    return {
        str(path.relative_to(workspace)): (path.is_dir(), path.read_bytes() if path.is_file() else b"")
        for path in workspace.rglob("*")
    }


@pytest.mark.parametrize("on_python_path", [False, True])
def test_demo_enumerates_malicious_metadata_but_never_loads_or_starts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on_python_path: bool
) -> None:
    workspace = tmp_path / "workspace"
    plugin_path = workspace / "installed-plugins"
    plugin_path.mkdir(parents=True, mode=0o700)
    suffix = uuid.uuid4().hex
    name = "demo-malicious-" + suffix
    module = "ngn_demo_malicious_" + suffix
    probe_name = "ngn_demo_probe_" + suffix
    effects: list[str] = []
    probe = ModuleType(probe_name)
    probe.__dict__["effects"] = effects
    monkeypatch.setitem(sys.modules, probe_name, probe)
    distribution = plugin_path / f"{module}-1.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    (distribution / "entry_points.txt").write_text(f"[nagents.channels]\n{name} = {module}:plugin\n")
    (plugin_path / f"{module}.py").write_text(
        "import asyncio\n"
        f"from {probe_name} import effects\n"
        "from nagents.channels.types import Channel, ChannelPlugin, ChannelDelivery\n"
        "effects.append('import')\n"
        "class Malicious(Channel):\n"
        "    def __init__(self, name): self.name = name\n"
        "    async def open(self): effects.append('open')\n"
        "    async def listen(self, receive):\n"
        "        effects.append('listen')\n"
        "        await asyncio.Event().wait()\n"
        "    async def send(self, message):\n"
        "        effects.append('send')\n"
        "        return ChannelDelivery(('sent',))\n"
        "    async def activity(self, event): effects.append('activity')\n"
        "    async def close(self): effects.append('close')\n"
        "def factory(config):\n"
        "    effects.append('factory')\n"
        "    return Malicious(config['name'])\n"
        "def setup(harness): effects.append('harness-plugin')\n"
        "plugin = ChannelPlugin('Malicious descriptor', 'Never execute in demo', "
        "{'type':'object','properties':{'token':{'type':'string','writeOnly':True}}}, factory)\n"
    )
    monkeypatch.setenv("NGN_CHANNEL_PLUGIN_PATH", str(plugin_path))
    if on_python_path:
        monkeypatch.syspath_prepend(str(plugin_path))
    original_load = metadata.EntryPoint.load

    def load(entry: metadata.EntryPoint) -> object:
        effects.append("entrypoint-load")
        # A regression must not import unrelated installed packages in this test.
        assert entry.name == name
        return original_load(entry)

    monkeypatch.setattr(metadata.EntryPoint, "load", load)
    network = Mock(side_effect=AssertionError("Demo attempted a live model call"))
    shell = Mock(side_effect=AssertionError("Demo attempted shell execution"))
    monkeypatch.setattr(Provider, "generate", network)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", shell)
    config = HarnessConfig(
        workspace=workspace, data_dir=tmp_path / "state", demo=True, auth="api-key", plugins=(f"{module}:setup",)
    )

    async def seed() -> tuple[Path, Path]:
        harness = Harness(config)
        try:
            await harness.initialize()
            store = RoutingStore(harness.agent.session.db_path)
            await store.initialize()
            await store.receive(
                "saved",
                _envelope("saved", ChannelMessage("queued", "chat", "sender", "pending input")),
                harness.session_id,
                None,
            )
            await store.receive(
                "saved",
                _envelope("saved", ChannelMessage("command", "chat", "sender", "/session main")),
                harness.session_id,
                ChannelCommand("session", "main"),
            )
            catalog = ChannelCatalog(config.data_dir, harness.agent.session.db_path, allow_plugins=False)
            catalog.path.parent.mkdir(mode=0o700)
            catalog.path.write_text(
                SavedCatalog(
                    revision="saved-revision",
                    connections={
                        "saved": Connection(
                            plugin=name,
                            enabled=True,
                            config={},
                            secrets={"token": "saved-test-secret"},
                            main_session_id=harness.session_id,
                        ),
                    },
                ).model_dump_json()
            )
            catalog.path.chmod(0o600)
            return harness.agent.session.db_path, catalog.path
        finally:
            await harness.close()

    database, credentials = asyncio.run(seed())
    saved = credentials.read_bytes()
    before = fingerprint(workspace)
    assets = tmp_path / "static"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("demo fixture")
    app = create_app(config, assets=assets)
    with TestClient(app, base_url=URL) as client:
        state: WebState = app.state.web
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["demo"] is True
        headers = {"Origin": URL, "X-Ngn-Token": bootstrap["token"]}
        response = client.get("/api/channels", headers=headers)
        assert response.status_code == 200
        listing = response.json()
        plugin = next(plugin for plugin in listing["plugins"] if plugin["id"] == name)
        assert plugin["schema"] == {"type": "object"} and plugin["version"] == "1.0"
        connection = listing["connections"][0]
        assert connection["enabled"] is True and connection["status"] == "disabled"
        assert "OFFLINE DEMO" in connection["error"] and "saved-test-secret" not in response.text
        assert not state.channels.channels and not state.channels.sources and not state.harness.loaded_plugins
        for _ in range(2):
            refreshed = client.post("/api/channels/refresh", json={}, headers=headers)
            assert refreshed.status_code == 200 and refreshed.json() == listing
        for enabled in (True, False):
            blocked = client.put(
                "/api/channels/saved",
                headers=headers,
                json={
                    "revision": listing["revision"],
                    "plugin": name,
                    "enabled": enabled,
                    "config": {},
                    "secrets": {"token": "replacement"},
                },
            )
            assert blocked.status_code == 501 and "OFFLINE DEMO" in blocked.text
        blocked = client.request(
            "DELETE", "/api/channels/saved", headers=headers, json={"revision": listing["revision"]}
        )
        assert blocked.status_code == 501
        with pytest.raises(HTTPException) as error:
            state.channels.catalog.plugin(name)
        assert error.value.status_code == 501

        message_id = str(uuid.uuid4())
        web_session = connection["main_session_id"]  # This root has a persisted channel binding.
        accepted = client.post(
            "/api/messages",
            headers=headers,
            json={
                "session_id": web_session,
                "prompt": "demo inspection",
                "message_id": message_id,
            },
        )
        assert accepted.status_code == 200

        async def completed() -> None:
            async with asyncio.timeout(10):
                while True:
                    async with aiosqlite.connect(database) as db:
                        cursor = await db.execute(
                            "SELECT status FROM ngn_web_inbox WHERE message_id = ?", (message_id,)
                        )
                        row = await cursor.fetchone()
                    if row is not None and row[0] == "completed" and state.active is None:
                        return
                    await asyncio.sleep(0.01)

        assert client.portal is not None
        client.portal.call(completed)
        snapshot = client.get(f"/api/sessions/{web_session}", headers=headers).json()
        assert "OFFLINE DEMO" in snapshot["history"][-1]["content"]
        assert effects == [] and module not in sys.modules
        assert credentials.read_bytes() == saved and fingerprint(workspace) == before
        if not on_python_path:
            assert str(plugin_path) not in sys.path
    assert effects == []
    network.assert_not_called()
    shell.assert_not_called()

    async def pending() -> list[str]:
        async with aiosqlite.connect(database) as db:
            cursor = await db.execute("SELECT status FROM ngn_web_inbox WHERE channel = 'saved' ORDER BY id")
            return [str(row[0]) for row in await cursor.fetchall()]

    assert asyncio.run(pending()) == ["queued", "queued"]
    assert credentials.read_bytes() == saved and fingerprint(workspace) == before


def test_demo_metadata_discovery_does_not_create_workspace_plugin_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "not-installed"
    monkeypatch.setenv("NGN_CHANNEL_PLUGIN_PATH", str(target))
    load = Mock(side_effect=AssertionError("Demo descriptor import"))
    monkeypatch.setattr(metadata.EntryPoint, "load", load)
    catalog = ChannelCatalog(tmp_path / "state", tmp_path / "state" / "sessions.db", allow_plugins=False)
    catalog.initialize()
    catalog.discover(descriptors=True)
    assert not target.exists() and not catalog.path.parent.exists()
    assert str(target) not in sys.path
    load.assert_not_called()
