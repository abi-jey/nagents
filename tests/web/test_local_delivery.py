"""Real web Harness sends, authenticated assets, and restart/ownership recovery."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.channels.types import ChannelError
from nagents.designer.runtime import DesignProvider
from nagents.designer.schema import STARTER
from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.web.local_delivery import MEDIA_TYPES
from nagents.web.local_delivery import valid_media
from tests.support.channels import site

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.channels import Site
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix

PNG = b"\x89PNG\r\n\x1a\nfixture-image"
WAV = b"RIFF\x18\0\0\0WAVEfixture-audio"
MP4 = b"\0\0\0\x18ftypisomfixture-video"


def deliveries(app: Site, root: str) -> list[dict[str, object]]:
    rows = cast("list[dict[str, object]]", app.history(root)["history"])
    return [delivery for row in rows for delivery in cast("list[dict[str, object]]", row.get("deliveries", []))]


def send(
    app: Site, *, files: tuple[str, ...] = (), allow: bool = True, retry: bool = False, switch_to: str = ""
) -> list[dict[str, object]]:
    requests = 0

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        nonlocal requests
        requests += 1
        if requests == 1 or (retry and requests == 2):
            yield ToolCallEvent(
                id="reused",
                name="channel_send",
                arguments={
                    "channel": "builtin.web",
                    "destination": app.main,
                    "text": "explicit",
                    "attachments": list(files),
                },
            )
            if retry and requests == 1:
                yield ErrorEvent(message="try again", recoverable=True)
        else:
            yield TextDoneEvent(text="ordinary answer")

    app.providers[0].script = script
    records: list[dict[str, object]] = []
    with app.socket() as socket:
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        assert socket.receive_json()["type"] == "snapshot"
        app.submit("deliver")
        while True:
            frame = socket.receive_json()
            record = frame.get("record", {})
            if not record:
                continue
            records.append(record)
            if record.get("event") == "approval":
                if switch_to:
                    assert (
                        app.client.post(
                            "/api/sessions/resume", headers=app.headers, json={"session_id": switch_to}
                        ).status_code
                        == 200
                    )
                response = app.client.post(
                    "/api/approval",
                    headers=app.headers,
                    json={
                        "run_id": record["run_id"],
                        "approval_id": record["approval_id"],
                        "call_id": record["id"],
                        "decision": "allow" if allow else "deny",
                    },
                )
                assert response.status_code == 200, response.text
            if record.get("event") == "run_finished":
                break
    app.idle()
    return records


@pytest.mark.parametrize("retry", [False, True])
def test_real_send_restart_assets_replay_and_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry: bool) -> None:
    samples = {"image.png": PNG, "sound.wav": WAV, "clip.mp4": MP4}
    for name, data in samples.items():
        (tmp_path / name).write_bytes(data)
    with site(tmp_path, monkeypatch) as app:
        records = send(app, files=tuple(samples), retry=retry)
        assert records[-1]["status"] == "completed"
        saved = deliveries(app, app.main)
        assert len(saved) == 1
        assert saved[0]["text"] == "explicit"
        assert len([record for record in records if record.get("event") == "local_delivery"]) == 1
        assert "fixture-image" not in json.dumps(records)
        assert "assets" not in next(record for record in records if record.get("event") == "local_delivery")
        assert sum(record.get("event") == "transcript_abandoned" for record in records) == int(retry)
        for _ in range(2):
            with app.socket() as socket:
                socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0, "epoch": "lost"})
                restored = socket.receive_json()["snapshot"]
                assert sum(len(row.get("deliveries", [])) for row in restored["history"]) == 1
        root = app.main
        assets = cast("list[dict[str, object]]", saved[0]["assets"])
        paths = [
            f"/api/sessions/{root}/deliveries/{saved[0]['delivery_id']}/assets/{asset['asset_id']}" for asset in assets
        ]
        for path, asset in zip(paths, assets, strict=True):
            assert app.client.get(path).status_code == 403
            response = app.client.get(path, headers=app.headers)
            assert response.status_code == 200
            assert response.content == samples[str(asset["filename"])]
            assert response.headers["content-type"] == asset["media_type"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "img-src 'self' blob:" in response.headers["content-security-policy"]
            assert "media-src blob:" in response.headers["content-security-policy"]
            assert app.client.get(path + "?token=bad", headers=app.headers).status_code == 400
            assert app.client.get(path.replace(root, "other-root"), headers=app.headers).status_code == 404
        for name in samples:
            (tmp_path / name).unlink()
    with site(tmp_path, monkeypatch) as app:
        assert deliveries(app, root) == saved
        for path in paths:
            assert app.client.get(path, headers=app.headers).status_code == 200
        # External ownership restricts new sends, never already committed history.
        app.configure(main_session_id=root)
        app.emit("/session main")
        app.idle()
        assert (
            app.client.post("/api/sessions/resume", headers=app.headers, json={"session_id": root}).status_code == 200
        )
        assert deliveries(app, root) == saved
        assert app.client.portal is not None
        assert [item["name"] for item in app.client.portal.call(app.state.channels.channel_list)] == ["fixture"]
        for path in paths:
            assert app.client.get(path, headers=app.headers).status_code == 200
        result = app.client.request("DELETE", f"/api/sessions/{root}", headers=app.headers, json={})
        assert result.status_code == 200, result.text
        deletion_id = result.json()["trash"]["deletion_id"]
        for path in paths:
            assert app.client.get(path, headers=app.headers).status_code == 404
        result = app.client.post(f"/api/trash/{root}/restore", headers=app.headers, json={"deletion_id": deletion_id})
        assert result.status_code == 200, result.text
        assert deliveries(app, root) == saved
        for path in paths:
            assert app.client.get(path, headers=app.headers).status_code == 200
        result = app.client.request("DELETE", f"/api/sessions/{root}", headers=app.headers, json={"permanent": True})
        assert result.status_code == 200, result.text
        for path in paths:
            assert app.client.get(path, headers=app.headers).status_code == 404


@pytest.mark.parametrize("case", ["denied", "txt", "svg", "fake-png"])
def test_rejection_has_no_partial_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    name = {"txt": "note.txt", "svg": "active.svg", "fake-png": "fake.png"}.get(case, "")
    if name:
        (tmp_path / name).write_text("<svg onload='alert(1)'></svg>")
    with site(tmp_path, monkeypatch) as app:
        send(app, files=(name,) if name else (), allow=case != "denied")
        assert deliveries(app, app.main) == []
        assert app.client.portal is not None

        async def receive(message: object) -> None:
            raise AssertionError("No listener")

        with pytest.raises(ChannelError, match="host-managed"):
            app.client.portal.call(app.state.channels.local.listen, receive)
        assert not app.state.channels.sources
        assert "builtin.web" not in app.state.channels.catalog.connections


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
def test_active_bytes_never_pass_declared_media(media_type: str) -> None:
    assert not valid_media(media_type, b"<html><script>alert(1)</script></html>")
    assert not valid_media(media_type, b"<svg onload='alert(1)'/>")


@pytest.mark.parametrize("designed", [False, True])
def test_executing_root_survives_selection_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, designed: bool
) -> None:
    with site(tmp_path, monkeypatch) as app:
        root = app.main
        other = app.client.post("/api/sessions/new", headers=app.headers, json={}).json()["session_id"]
        if designed:
            assert app.client.portal is not None

            async def pin() -> None:
                def save(db: sqlite3.Connection) -> None:
                    db.execute(
                        "INSERT OR REPLACE INTO ngn_design_sessions VALUES (?, ?, ?)", (root, "assistant", STARTER)
                    )

                await app.state.channels.store._transaction(save)

            app.client.portal.call(pin)

            async def generate(self: DesignProvider, messages: list[Message], **kwargs: object) -> AsyncIterator[Event]:
                if messages[-1].role != "tool":
                    yield ToolCallEvent(
                        id="designed",
                        name="channel_send",
                        arguments={
                            "channel": "builtin.web",
                            "destination": root,
                            "text": "designed delivery",
                        },
                    )
                else:
                    yield TextDoneEvent(text="ordinary answer")

            monkeypatch.setattr(DesignProvider, "generate", generate)
        events = send(app, switch_to=other)
        assert events[-1]["status"] == "completed"
        assert len(deliveries(app, root)) == 1
        assert not deliveries(app, other)
        assert app.state.selected_session_id == other
        assert app.state.running_harness is app.state.harness


def test_owned_root_rejects_local_send_but_keeps_external_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        app.emit("/session main")
        app.idle()
        events = send(app)
        assert len([event for event in events if event.get("event") == "approval"]) == 1
        assert not deliveries(app, app.main)
        assert not any(event.get("event") == "local_delivery" for event in events)
