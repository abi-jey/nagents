from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from examples.a2a import keycloak_fixture

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.skipif(os.name != "posix", reason="fixture helper requires POSIX ownership and modes")
def test_metadata_write_failure_removes_only_owned_container_and_fixture_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "fixture"
    directory.mkdir(mode=0o700)
    unrelated = directory / "keep.txt"
    unrelated.write_text("unrelated state", encoding="utf-8")
    container_id = "a" * 64
    owners: list[str] = []
    docker_calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, env: dict[str, str] | None = None) -> str:
        del env
        docker_calls.append(arguments)
        if arguments[0] == "run":
            label_argument = arguments[arguments.index("--label") + 1]
            owners.append(label_argument.removeprefix(f"{keycloak_fixture.LABEL}="))
            return container_id
        if arguments[0] == "inspect":
            assert arguments[-1] == container_id
            assert owners
            return json.dumps({keycloak_fixture.LABEL: owners[-1]})
        if arguments[:2] == ("rm", "-f"):
            return ""
        raise AssertionError(f"Unexpected Docker command: {arguments[0]}")

    def fake_prepare(target: Path) -> dict[str, str]:
        (target / "realm.json").write_text("{}", encoding="utf-8")
        return {}

    original_write_private = keycloak_fixture.write_private

    def fail_container_metadata(path: Path, text: str) -> None:
        if path.name == "container.json":
            raise OSError("simulated metadata write failure")
        original_write_private(path, text)

    monkeypatch.setattr(keycloak_fixture, "docker", fake_docker)
    monkeypatch.setattr(keycloak_fixture, "prepare", fake_prepare)
    monkeypatch.setattr(keycloak_fixture, "write_private", fail_container_metadata)

    with pytest.raises(OSError, match="simulated metadata write failure"):
        keycloak_fixture.start(directory)

    assert ("rm", "-f", container_id) in docker_calls
    assert unrelated.read_text(encoding="utf-8") == "unrelated state"
    assert not any((directory / name).exists() for name in keycloak_fixture.GENERATED)
    assert capsys.readouterr().out == ""
