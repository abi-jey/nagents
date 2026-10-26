"""CLI boundaries for the separately configured A2A server."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import uvicorn

from nagents.a2a.server import cli as a2a_cli
from nagents.cli import main

if TYPE_CHECKING:
    from pathlib import Path
from nagents.harness.config import HarnessConfig


def write_a2a_config(path: Path, *, public_url: str = "http://127.0.0.1:8768") -> Path:
    path.write_text(
        json.dumps(
            {
                "public_url": public_url,
                "loopback_http": public_url.startswith("http://"),
                "agent": "assistant",
                "issuer": {"issuer": "https://issuer.example", "audience": "nagents-a2a"},
                "data_dir": str(path.parent / "service-state"),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_cli_a2a_dispatch_preserves_exact_config_path_and_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    configuration = write_a2a_config(tmp_path / "operator-a2a.json")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    calls: list[tuple[HarnessConfig, dict[str, object]]] = []

    def serve(config: HarnessConfig, **kwargs: object) -> None:
        calls.append((config, kwargs))

    monkeypatch.setattr(a2a_cli, "serve", serve)
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "a2a",
                "serve",
                "--a2a-config",
                str(configuration),
                "--host",
                "127.0.0.1",
                "--port",
                "9911",
                "--agent",
                "assistant",
                "--tls-certfile",
                "operator-cert.pem",
                "--tls-keyfile",
                "operator-key.pem",
                "--trusted-proxies",
                "192.0.2.10,2001:db8::10",
            ]
        )
        == 0
    )
    assert len(calls) == 1
    harness_config, arguments = calls[0]
    assert harness_config.workspace == workspace.resolve()
    assert arguments == {
        "configuration": configuration,
        "host": "127.0.0.1",
        "port": 9911,
        "tls_certfile": "operator-cert.pem",
        "tls_keyfile": "operator-key.pem",
        "trusted_proxies": "192.0.2.10,2001:db8::10",
        "agent_override": "assistant",
    }


@pytest.mark.parametrize("invalid", ["missing", "json"])
def test_cli_reports_missing_or_invalid_a2a_config_without_starting_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], invalid: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "invalid-a2a.json"
    if invalid == "json":
        config.write_text("{", encoding="utf-8")
    else:
        config = tmp_path / "missing-a2a.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    started: list[bool] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: started.append(True))

    assert main(["--workspace", str(workspace), "a2a", "serve", "--a2a-config", str(config)]) == 2
    assert not started
    assert "ngn:" in capsys.readouterr().err


def test_a2a_serve_uses_real_config_validation_and_fixed_uvicorn_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = write_a2a_config(tmp_path / "operator-a2a.json")
    harness_config = HarnessConfig(workspace=tmp_path.resolve(), data_dir=(tmp_path / "harness-state").resolve())
    calls: list[tuple[object, dict[str, object]]] = []

    def capture(app: object, **kwargs: object) -> None:
        calls.append((app, kwargs))

    monkeypatch.setattr(uvicorn, "run", capture)
    a2a_cli.serve(
        harness_config,
        configuration=configuration,
        host="127.0.0.1",
        port=8768,
        tls_certfile="certificate.pem",
        tls_keyfile="private-key.pem",
        trusted_proxies="192.0.2.8,2001:db8::8",
        agent_override="assistant",
    )
    assert len(calls) == 1
    app, arguments = calls[0]
    assert app is not None
    assert arguments == {
        "host": "127.0.0.1",
        "port": 8768,
        "workers": 1,
        "ssl_certfile": "certificate.pem",
        "ssl_keyfile": "private-key.pem",
        "proxy_headers": True,
        "forwarded_allow_ips": "192.0.2.8,2001:db8::8",
        "access_log": False,
    }


@pytest.mark.parametrize(
    ("host", "tls_certfile", "tls_keyfile", "message"),
    [
        ("0.0.0.0", "", "", "loopback address"),
        ("127.0.0.1", "certificate.pem", "", "both --tls-certfile and --tls-keyfile"),
        ("127.0.0.1", "", "private-key.pem", "both --tls-certfile and --tls-keyfile"),
    ],
)
def test_a2a_serve_rejects_unsafe_bind_and_unpaired_tls_without_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    tls_certfile: str,
    tls_keyfile: str,
    message: str,
) -> None:
    configuration = write_a2a_config(tmp_path / "operator-a2a.json")
    calls: list[bool] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: calls.append(True))

    with pytest.raises(ValueError, match=message):
        a2a_cli.serve(
            HarnessConfig(workspace=tmp_path.resolve(), data_dir=(tmp_path / "harness-state").resolve()),
            configuration=configuration,
            host=host,
            port=8768,
            tls_certfile=tls_certfile,
            tls_keyfile=tls_keyfile,
        )
    assert not calls


def test_a2a_serve_requires_agent_override_to_match_exported_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = write_a2a_config(tmp_path / "operator-a2a.json")
    calls: list[bool] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: calls.append(True))

    with pytest.raises(ValueError, match="must match the exported profile"):
        a2a_cli.serve(
            HarnessConfig(workspace=tmp_path.resolve(), data_dir=(tmp_path / "harness-state").resolve()),
            configuration=configuration,
            host="127.0.0.1",
            port=8768,
            agent_override="reviewer",
        )
    assert not calls
