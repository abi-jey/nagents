"""A recovered editor keeps its dictation scope independently of legacy selection."""

from pathlib import Path

import pytest
from starlette.requests import Request

from nagents.harness.config import HarnessConfig
from tests.test_web_channels import site

pytestmark = pytest.mark.requires_posix


def test_verified_editor_dictation_scope_survives_a_changed_shared_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        admitted: list[str] = []

        async def transcribe(request: Request, config: HarnessConfig) -> dict[str, str]:
            assert await request.body() == b"synthetic audio"
            admitted.append(request.headers["x-ngn-session"])
            return {"text": "Editable synthetic transcript"}

        monkeypatch.setattr(app.state.dictation, "transcribe", transcribe)
        old = app.main
        created = app.client.post("/api/sessions/new", headers=app.headers, json={})
        assert created.status_code == 200
        selected = str(created.json()["session_id"])
        assert old != selected
        headers = {
            **app.headers,
            "Content-Type": "audio/wav",
            "X-Ngn-Session": old,
            "X-Ngn-Settings-Revision": app.state.settings.revision,
        }

        def upload(target: str = old) -> int:
            return int(
                app.client.post(
                    "/api/dictation/transcribe",
                    headers={**headers, "X-Ngn-Session": target},
                    content=b"synthetic audio",
                ).status_code
            )

        assert upload() == 409 and not admitted
        before = app.history(old)["history"]
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": old, "after": 0})
            assert socket.receive_json()["type"] == "snapshot"
            assert upload() == 200
            assert upload("ngn-unknown") == 409
            assert app.state.selected_session_id == selected
            assert app.history(old)["history"] == before
            assert not app.providers[0].requests
        assert upload() == 409
        assert upload(selected) == 200
        assert admitted == [old, selected]
