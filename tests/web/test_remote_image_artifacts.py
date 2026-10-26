"""Owned remote image history is served as authenticated metadata and bytes."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.artifacts import ImageArtifactReference
from nagents.harness.remote.artifacts import image_references
from nagents.harness.remote.artifacts import referenced_images
from nagents.types import ContentPart
from nagents.types import ImageContent
from nagents.types import Message
from tests.support.channels import site

if TYPE_CHECKING:
    from tests.support.channels import Site

pytestmark = pytest.mark.requires_posix


def add_image_history(app: Site, data: bytes, *, filename: str = "cover.png") -> tuple[int, str]:
    media_type = "image/jpeg" if filename.endswith(".jpg") else "image/png"
    image = ImageContent(
        base64.b64encode(data).decode("ascii"),
        media_type,
        filename=filename,
        artifact_id="remote-cover",
        artifact_part=0,
    )

    async def add() -> int:
        return await app.state.history.add_message(app.main, Message(role="assistant", content=[image]))

    assert app.client.portal is not None
    return app.client.portal.call(add), app.main


def test_assistant_image_history_has_metadata_reference_and_authenticated_exact_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = (Path(__file__).parents[1] / "fixtures" / "a2a-images" / "red.png").read_bytes()
    with site(tmp_path, monkeypatch) as app:
        assert app.client.portal is not None
        history_id, root = add_image_history(app, expected)
        response = app.client.get(f"/api/sessions/{root}", headers=app.headers)
        assert response.status_code == 200
        payload = response.json()
        serialized = json.dumps(payload)
        assert base64.b64encode(expected).decode("ascii") not in serialized
        history = cast("list[dict[str, object]]", payload["history"])
        row = next(item for item in history if item.get("history_id") == str(history_id))
        artifacts = cast("list[dict[str, object]]", row["parts"])
        assert artifacts == [
            {
                "type": "artifact_image",
                "artifact_id": "remote-cover",
                "artifact_part": 0,
                "filename": "cover.png",
                "media_type": "image/png",
                "byte_length": len(expected),
                "path": f"/api/sessions/{root}/artifacts/{history_id}/0",
                "caption": "",
            }
        ]
        path = artifacts[0]["path"]
        assert isinstance(path, str)
        assert app.client.get(path).status_code in {401, 403}
        downloaded = app.client.get(path, headers=app.headers)
        assert downloaded.status_code == 200
        assert downloaded.content == expected
        assert downloaded.headers["content-type"] == "image/png"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert "cover.png" in downloaded.headers["content-disposition"]

        async def read_as_owner() -> list[ImageContent]:
            messages = await app.state.history.get_history(root)
            references = image_references(messages[-1].content, root, history_id)
            return await referenced_images(app.state.history.db_path, root, root, references)

        owned = app.client.portal.call(read_as_owner)
        assert len(owned) == 1
        assert owned[0].base64_data == base64.b64encode(expected).decode("ascii")


def test_distinct_image_captions_survive_authenticated_history_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_dir = Path(__file__).parents[1] / "fixtures" / "a2a-images"
    png = (fixture_dir / "red.png").read_bytes()
    jpeg = (fixture_dir / "blue.jpg").read_bytes()
    with site(tmp_path, monkeypatch) as app:
        assert app.client.portal is not None

        async def add_captioned_images() -> tuple[int, str]:
            content: list[ContentPart] = [
                ImageContent(
                    base64.b64encode(png).decode("ascii"),
                    "image/png",
                    filename="first.png",
                    artifact_id="first",
                    artifact_part=1,
                    caption="First image caption",
                ),
                ImageContent(
                    base64.b64encode(jpeg).decode("ascii"),
                    "image/jpeg",
                    filename="second.jpg",
                    artifact_id="second",
                    artifact_part=2,
                    caption="Second image caption",
                ),
            ]
            return await app.state.history.add_message(app.main, Message(role="assistant", content=content)), app.main

        history_id, root = app.client.portal.call(add_captioned_images)
        response = app.client.get(f"/api/sessions/{root}", headers=app.headers)
        assert response.status_code == 200
        history = cast("list[dict[str, object]]", response.json()["history"])
        row = next(item for item in history if item.get("history_id") == str(history_id))
        references = cast("list[dict[str, object]]", row["parts"])
        assert [item["caption"] for item in references] == ["First image caption", "Second image caption"]
        assert base64.b64encode(png).decode("ascii") not in json.dumps(references)
        assert base64.b64encode(jpeg).decode("ascii") not in json.dumps(references)
        assert [app.client.get(str(item["path"]), headers=app.headers).content for item in references] == [png, jpeg]

        async def read_as_owner() -> tuple[list[str], list[str]]:
            messages = await app.state.history.get_history(root)
            projected = image_references(messages[-1].content, root, history_id)
            owned = await referenced_images(app.state.history.db_path, root, root, projected)
            return [item.caption for item in projected], [item.caption for item in owned]

        projected_captions, stored_captions = app.client.portal.call(read_as_owner)
        assert projected_captions == ["First image caption", "Second image caption"]
        assert stored_captions == projected_captions


def test_image_asset_does_not_cross_root_scope_or_survive_deleted_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = (Path(__file__).parents[1] / "fixtures" / "a2a-images" / "blue.jpg").read_bytes()
    with site(tmp_path, monkeypatch) as app:
        assert app.client.portal is not None
        history_id, root = add_image_history(app, expected, filename="cover.jpg")
        foreign_root = "ngn-foreign-root"
        path = f"/api/sessions/{root}/artifacts/{history_id}/0"
        assert (
            app.client.get(f"/api/sessions/{foreign_root}/artifacts/{history_id}/0", headers=app.headers).status_code
            == 404
        )
        assert app.client.get(f"/api/sessions/{root}/artifacts/{history_id}/1", headers=app.headers).status_code == 404

        async def orphan_history() -> int:
            await app.state.history.get_or_create_session("ngn-unowned-child", "harness")
            data = base64.b64encode(expected).decode("ascii")
            image = ImageContent(data, "image/jpeg", filename="cover.jpg", artifact_id="foreign", artifact_part=0)
            return await app.state.history.add_message("ngn-unowned-child", Message(role="assistant", content=[image]))

        orphan_id = app.client.portal.call(orphan_history)
        orphan = app.client.get(f"/api/sessions/{root}/artifacts/{orphan_id}/0", headers=app.headers)
        assert orphan.status_code == 404
        deleted = app.client.request("DELETE", f"/api/sessions/{root}", headers=app.headers, json={})
        assert deleted.status_code == 200
        assert app.client.get(path, headers=app.headers).status_code == 404
        restored = app.client.post(
            f"/api/trash/{root}/restore",
            headers=app.headers,
            json={"deletion_id": deleted.json()["trash"]["deletion_id"]},
        )
        assert restored.status_code == 200
        assert app.client.get(path, headers=app.headers).content == expected
        purged = app.client.request("DELETE", f"/api/sessions/{root}", headers=app.headers, json={"permanent": True})
        assert purged.status_code == 200
        assert app.client.get(path, headers=app.headers).status_code == 404


def test_referenced_images_rejects_forged_foreign_root_and_orphan_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = (Path(__file__).parents[1] / "fixtures" / "a2a-images" / "red.png").read_bytes()
    with site(tmp_path, monkeypatch) as app:
        assert app.client.portal is not None
        _, root = add_image_history(app, expected)
        with sqlite3.connect(app.state.history.db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS nagents_agent_sessions (storage_id TEXT, root_session_id TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS harness_sessions (id TEXT)")

        async def add_foreign(session_id: str, artifact_id: str) -> int:
            await app.state.history.get_or_create_session(session_id, "harness")
            image = ImageContent(
                base64.b64encode(expected).decode("ascii"),
                "image/png",
                filename="cover.png",
                artifact_id=artifact_id,
                artifact_part=0,
            )
            return await app.state.history.add_message(session_id, Message(role="assistant", content=[image]))

        orphan_history = app.client.portal.call(add_foreign, "ngn-unowned-orphan", "orphan-image")
        foreign_root = "ngn-foreign-root"
        foreign_history = app.client.portal.call(add_foreign, foreign_root, "foreign-root-image")
        forged = (
            ImageArtifactReference(
                artifact_id="orphan-image",
                artifact_part=0,
                filename="cover.png",
                media_type="image/png",
                byte_length=len(expected),
                path=f"/api/sessions/{root}/artifacts/{orphan_history}/0",
                caption="",
            ),
            ImageArtifactReference(
                artifact_id="foreign-root-image",
                artifact_part=0,
                filename="cover.png",
                media_type="image/png",
                byte_length=len(expected),
                path=f"/api/sessions/{root}/artifacts/{foreign_history}/0",
                caption="",
            ),
        )

        async def read_forged(reference: ImageArtifactReference) -> list[ImageContent]:
            return await referenced_images(app.state.history.db_path, root, root, (reference,))

        for reference in forged:
            with pytest.raises(ValueError, match="history is unavailable"):
                app.client.portal.call(read_forged, reference)


def test_image_reference_model_exposes_metadata_only() -> None:
    reference = ImageArtifactReference(
        artifact_id="image-1",
        artifact_part=2,
        filename="image.png",
        media_type="image/png",
        byte_length=128,
        path="/api/sessions/root/artifacts/5/2",
        caption="A caption",
    )
    assert reference.type == "artifact_image"
    assert reference.byte_length == 128
    assert not hasattr(reference, "data")


def test_remote_image_bytes_survive_web_history_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = (Path(__file__).parents[1] / "fixtures" / "a2a-images" / "red.png").read_bytes()
    with site(tmp_path, monkeypatch) as app:
        assert app.client.portal is not None
        history_id, root = add_image_history(app, expected)
    with site(tmp_path, monkeypatch) as restarted:
        response = restarted.client.get(f"/api/sessions/{root}/artifacts/{history_id}/0", headers=restarted.headers)
        assert response.status_code == 200
        assert response.content == expected
