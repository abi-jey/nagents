"""Staged upload ownership, atomic admission and real native model-input execution."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import TYPE_CHECKING
from typing import cast

import pytest
from fastapi import HTTPException

from nagents.designer.schema import STARTER
from nagents.designer.schema import parse
from nagents.designer.schema import serialize
from nagents.media import MediaCapabilities
from nagents.session import SessionManager
from nagents.types import DocumentContent
from nagents.types import ImageContent
from nagents.web.upload_store import expire
from nagents.web.uploads import supported
from tests.support.channels import site
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.web.test_web_deletion import quiet
from tests.web.test_web_deletion import rows

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.web.service import WebState

PNG = b"\x89PNG\r\n\x1a\n" + b"image-content-not-websocket-data"


@pytest.mark.requires_posix
@pytest.mark.parametrize("prompt", ["", "Describe this image"])
def test_upload_waits_for_explicit_send_and_native_history_is_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        id = str(uuid.uuid4())
        path = f"/api/sessions/{app.main}/uploads/{id}"
        headers = {**app.headers, "Content-Type": "image/png", "X-Ngn-Filename": "screenshot.png"}
        reply = app.client.post(path, headers=headers, content=PNG)
        assert reply.status_code == 200, reply.text
        assert not app.providers[0].requests
        assert app.history(app.main)["history"] == []
        body = {"session_id": app.main, "message_id": str(uuid.uuid4()), "prompt": prompt, "attachments": [id]}
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 200
        app.idle()
        assert len(app.providers[0].requests) == 1
        message = next(message for message in app.providers[0].requests[0] if message.role == "user")
        assert isinstance(message.content, list)
        image = next(part for part in message.content if isinstance(part, ImageContent))
        assert base64.b64decode(image.base64_data) == PNG
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            frame = socket.receive_json()
            assert "data_base64" not in json.dumps(frame)
            assert base64.b64encode(PNG).decode() not in json.dumps(frame)
        history = cast("list[dict[str, object]]", app.history(app.main)["history"])
        assert history[0]["content"] == prompt
        assert app.roots()[app.main] == (prompt or "Attachments: screenshot.png")
        assert history[0]["parts"] == []
        assert cast("list[dict[str, object]]", history[0]["uploads"])[0]["filename"] == "screenshot.png"
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 200
        app.idle()
        assert len(app.providers[0].requests) == 1
        assert app.client.post("/api/messages", headers=app.headers, json={**body, "attachments": []}).status_code in {
            409,
            422,
        }


def test_staging_auth_bounds_scope_and_atomic_dedup(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            state.harness.config.demo = False
            root = state.selected_session_id
            other = await state.harness.new_session()
            id = str(uuid.uuid4())
            path = f"/api/sessions/{root}/uploads/{id}"
            media = {**headers, "Content-Type": "image/png", "X-Ngn-Filename": "test.png"}
            assert (await client.post(path, content=PNG)).status_code == 403
            assert (await client.post(path, headers=media, content=PNG)).status_code == 200
            assert (await client.post(path, headers=media, content=PNG)).status_code == 200
            assert (await client.post(path, headers=media, content=PNG + b"changed")).status_code == 409
            body = {"session_id": root, "message_id": str(uuid.uuid4()), "prompt": "text", "attachments": [id]}
            assert (
                await client.post("/api/messages", headers=headers, json={**body, "session_id": other})
            ).status_code == 409
            assert (
                await client.post("/api/messages", headers=headers, json={**body, "attachments": [id, id]})
            ).status_code == 422
            assert not await rows(state, "SELECT id FROM ngn_web_inbox")
            assert (await client.post("/api/messages", headers=headers, json=body)).status_code == 200
            assert (await client.post("/api/messages", headers=headers, json=body)).status_code == 200
            changes: tuple[dict[str, object], ...] = (
                {"attachments": []},
                {"prompt": "different"},
                {"attachments": [str(uuid.uuid4())]},
            )
            for changed in changes:
                assert (
                    await client.post("/api/messages", headers=headers, json={**body, **changed})
                ).status_code == 409
            assert (
                await client.post("/api/messages", headers=headers, json={**body, "message_id": str(uuid.uuid4())})
            ).status_code == 409
            assert len(await rows(state, "SELECT id FROM ngn_web_inbox")) == 1
            assert (await client.request("DELETE", path, headers=headers, json={})).status_code == 200
            assert len(await rows(state, "SELECT upload_id FROM ngn_web_uploads")) == 1  # Accepted bytes are immutable.
            assert (await client.get(path, headers=headers)).status_code == 405  # Never serve uploaded active media.
            assert (await client.post(path + "?token=x", headers=media, content=PNG)).status_code == 400

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["cancel", "clear", "overflow"])
def test_streaming_upload_cancellation_clear_and_actual_byte_limit(tmp_path: Path, outcome: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            state.harness.config.demo = False
            entered, release = asyncio.Event(), asyncio.Event()

            async def content() -> AsyncIterator[bytes]:
                yield PNG
                entered.set()
                await release.wait()
                yield b"x" * (8 * 1024 * 1024) if outcome == "overflow" else b"tail"

            upload = asyncio.create_task(
                client.post(
                    f"/api/sessions/{state.selected_session_id}/uploads/{uuid.uuid4()}",
                    headers={**headers, "Content-Type": "image/png", "Content-Length": "1"},
                    content=content(),
                )
            )
            try:
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                    if outcome == "cancel":
                        upload.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await upload
                    else:
                        if outcome == "clear":
                            await state.history.clear_session(state.selected_session_id)
                        release.set()
                        reply = await upload
                        assert reply.status_code == (409 if outcome == "clear" else 413), reply.text
            finally:
                release.set()
                if not upload.done():
                    upload.cancel()
                await asyncio.gather(upload, return_exceptions=True)
            assert state.uploads.loading == 0
            assert not await rows(state, "SELECT upload_id FROM ngn_web_uploads")

    asyncio.run(run())


@pytest.mark.requires_posix
def test_pdf_native_support_and_owned_chat_does_not_forward(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"%PDF-1.7\nPDF-native-fixture"
    with site(tmp_path, monkeypatch) as app:
        # Resolve the actual configured adapter's declared media capabilities.
        app.providers[0].api = "messages"
        assert "application/pdf" in supported(app.state.harness)
        app.configure(auto_reply=True)
        app.emit("/session main")
        app.idle()
        previous_sends = len(app.channels[0].deliveries)
        id = str(uuid.uuid4())
        reply = app.client.post(
            f"/api/sessions/{app.main}/uploads/{id}",
            headers={**app.headers, "Content-Type": "application/pdf", "X-Ngn-Filename": "report.pdf"},
            content=data,
        )
        assert reply.status_code == 200, reply.text
        body = {"session_id": app.main, "message_id": str(uuid.uuid4()), "prompt": "", "attachments": [id]}
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 200
        app.idle()
        message = next(
            message
            for message in app.providers[0].requests[-1]
            if message.role == "user" and isinstance(message.content, list)
        )
        assert isinstance(message.content, list)
        document = next(part for part in message.content if isinstance(part, DocumentContent))
        assert document.title == "report.pdf" and base64.b64decode(document.base64_data) == data
        assert len(app.channels[0].deliveries) == previous_sends


@pytest.mark.parametrize("changed", ["missing", "provider"])
def test_admitted_attachments_are_rechecked_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            state.harness.config.demo = False
            root, id = state.selected_session_id, str(uuid.uuid4())
            assert (
                await client.post(
                    f"/api/sessions/{root}/uploads/{id}", headers={**headers, "Content-Type": "image/png"}, content=PNG
                )
            ).status_code == 200
            body = {
                "session_id": root,
                "message_id": str(uuid.uuid4()),
                "prompt": "with attachment",
                "attachments": [id],
            }
            assert (await client.post("/api/messages", headers=headers, json=body)).status_code == 200
            work = await state.channels.store.claim_work()
            assert work is not None and work.attachments == (id,)
            if changed == "missing":
                await state.history.clear_session(root)
            else:
                monkeypatch.setattr(
                    type(state.harness.agent.provider),
                    "supported_media_formats",
                    property(lambda self: MediaCapabilities()),
                )
            with pytest.raises(HTTPException):
                await state.uploads.content(work, state.harness)
            # A known admission is still an idempotent retry even after cleanup or
            # capability changes. It can never become a second text-only run.
            assert (await client.post("/api/messages", headers=headers, json=body)).status_code == 200
            assert (
                await client.post("/api/messages", headers=headers, json={**body, "attachments": []})
            ).status_code == 409

    asyncio.run(run())


@pytest.mark.parametrize(
    "case", ["empty", "svg", "disguised", "oversized", "encoding", "filename", "pdf", "concurrent"]
)
def test_invalid_or_unsupported_upload_creates_no_bytes(tmp_path: Path, case: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            state.harness.config.demo = False
            root = state.selected_session_id
            media = {**headers, "Content-Type": "image/png", "X-Ngn-Filename": "test.png"}
            data = PNG
            if case == "empty":
                data = b""
            if case == "svg":
                media["Content-Type"] = "image/svg+xml"
            if case == "disguised":
                data = b"<svg onload='alert(1)'/>"
            if case == "oversized":
                data = PNG + b"x" * (8 * 1024 * 1024)
            if case == "encoding":
                media["Content-Encoding"] = "gzip"
            if case == "filename":
                media["X-Ngn-Filename"] = "..%2Fevil.png"
            if case == "pdf":
                media["Content-Type"] = "application/pdf"
                data = b"%PDF-1.7"
            if case == "concurrent":
                state.uploads.loading = 4
            reply = await client.post(f"/api/sessions/{root}/uploads/{uuid.uuid4()}", headers=media, content=data)
            assert reply.status_code in {413, 415, 422, 429}, reply.text
            state.uploads.loading = 0
            assert not await rows(state, "SELECT upload_id FROM ngn_web_uploads")
            assert not await rows(state, "SELECT id FROM ngn_web_inbox")

    asyncio.run(run())


@pytest.mark.requires_posix
def test_queued_attachment_and_retry_identity_survive_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        assert app.client.portal is not None
        app.client.portal.call(quiet, app.state)
        root, id = app.main, str(uuid.uuid4())
        response = app.client.post(
            f"/api/sessions/{root}/uploads/{id}", headers={**app.headers, "Content-Type": "image/png"}, content=PNG
        )
        assert response.status_code == 200, response.text
        body = {"session_id": root, "message_id": str(uuid.uuid4()), "prompt": "restart input", "attachments": [id]}
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 200
        assert not app.providers[0].requests
    with site(tmp_path, monkeypatch) as app:
        app.idle()
        assert len(app.providers[0].requests) == 1
        original = next(message for message in app.providers[0].requests[0] if message.role == "user")
        assert isinstance(original.content, list) and any(isinstance(part, ImageContent) for part in original.content)
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 200
        app.idle()
        assert len(app.providers[0].requests) == 1
        assert (
            app.client.post("/api/messages", headers=app.headers, json={**body, "attachments": []}).status_code == 409
        )
        history = cast("list[dict[str, object]]", app.history(root)["history"])
        assert history[0]["uploads"] and not history[0]["parts"]


def test_pinned_design_capabilities_and_custom_adapter_gate(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            state.harness.config.demo = False
            root = state.selected_session_id
            assert "application/pdf" not in await state.uploads.capabilities(root)
            definition = parse(STARTER)
            definition.providers["primary"].type = "anthropic"
            definition.providers["primary"].api = "messages"
            source = serialize(definition)

            def pin(db: sqlite3.Connection) -> None:
                db.execute("INSERT OR REPLACE INTO ngn_design_sessions VALUES (?, ?, ?)", (root, "assistant", source))

            await state.channels.store._transaction(pin)
            assert "application/pdf" in await state.uploads.capabilities(root)
            response = await client.post(
                f"/api/sessions/{root}/uploads/{uuid.uuid4()}",
                headers={**headers, "Content-Type": "application/pdf"},
                content=b"%PDF-1.7",
            )
            assert response.status_code == 200, response.text
            await state.channels.store._transaction(lambda db: db.execute("DELETE FROM ngn_design_sessions").close())
            state.harness.agent.session = SessionManager(state.history.db_path)
            assert await state.uploads.capabilities(root) == ()
            # The ordinary adapter and text route are still usable.
            response = await client.post(
                "/api/messages",
                headers=headers,
                json={"session_id": root, "message_id": str(uuid.uuid4()), "prompt": "ordinary"},
            )
            assert response.status_code == 200, response.text

    asyncio.run(run())


@pytest.mark.parametrize("cleanup", ["remove", "expire", "clear", "delete", "trash"])
def test_staged_upload_cleanup(tmp_path: Path, cleanup: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            state.harness.config.demo = False
            root = state.selected_session_id
            id = str(uuid.uuid4())
            path = f"/api/sessions/{root}/uploads/{id}"
            reply = await client.post(path, headers={**headers, "Content-Type": "image/png"}, content=PNG)
            assert reply.status_code == 200, reply.text
            if cleanup == "remove":
                assert (await client.request("DELETE", path, headers=headers, json={})).status_code == 200
            elif cleanup == "expire":
                await state.channels.store._transaction(
                    lambda db: db.execute("UPDATE ngn_web_uploads SET expires_at = 0").close()
                )
                await state.channels.store._transaction(expire)
            elif cleanup == "clear":
                await state.history.clear_session(root)
            elif cleanup == "delete":
                await state.history.delete_session(root)
            else:
                assert (
                    await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})
                ).status_code == 200
            assert not await rows(state, "SELECT upload_id FROM ngn_web_uploads")

    asyncio.run(run())
