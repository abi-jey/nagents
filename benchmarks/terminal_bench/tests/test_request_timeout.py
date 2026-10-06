"""Named deadline variants keep shipping factories, auth routing, and cleanup."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import cast
from unittest.mock import AsyncMock

import aiohttp
import pytest
from benchmarks.terminal_bench import runner
from benchmarks.terminal_bench.prepare import DATASET_COMMIT
from benchmarks.terminal_bench.prepare import DATASET_REPO
from benchmarks.terminal_bench.prepare import HARBOR_COMMIT
from benchmarks.terminal_bench.prepare import HARBOR_VERSION
from benchmarks.terminal_bench.prepare import digest
from benchmarks.terminal_bench.prepare import job_config
from benchmarks.terminal_bench.prepare import reuse_runtime_bundle

from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.harness import Harness
from nagents.harness import providers
from nagents.provider import OpenAIProvider
from nagents.provider.openai import CodexCredentials
from nagents.types import Message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nagents.events import Event

pytestmark = pytest.mark.requires_posix


@pytest.fixture(autouse=True)
def isolated_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CODEX_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setattr(runner, "PRIVATE_ROOT", tmp_path / "private")
    monkeypatch.setattr(runner, "PRIVATE_CODEX_HOME", tmp_path / "codex-access")
    monkeypatch.setattr(aiohttp.ClientSession, "_request", AsyncMock(side_effect=AssertionError("No network calls")))


def synthetic_credentials() -> CodexCredentials:
    claims = {
        "exp": time.time() + 3600,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "synthetic-account",
            "chatgpt_compute_residency": "eu",
        },
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return CodexCredentials(f"synthetic.{payload}.signature", "synthetic-account", "eu")


class Response:
    status = 200
    headers: ClassVar[dict[str, str]] = {"Content-Type": "text/event-stream"}
    content_type = "text/event-stream"

    @property
    def content(self) -> Response:
        return self

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        yield b'data: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'

    async def __aenter__(self) -> Response:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_root_and_native_child_only_change_http_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = synthetic_credentials()
    captures: list[dict[str, object]] = []

    def request(session: aiohttp.ClientSession, url: str, **kwargs: object) -> Response:
        captures.append({"deadline": session.timeout.total, "url": url, **kwargs})
        return Response()

    monkeypatch.setattr(aiohttp.ClientSession, "post", request)
    for deadline in (None, 300):
        workspace = tmp_path / str(deadline)
        workspace.mkdir()
        config = runner.configuration(workspace, "gpt-6-astra", workspace / "private")
        if deadline is not None:
            runner.PRIVATE_CODEX_HOME.mkdir(mode=0o700)
            config = await runner.named_deadline(config, credentials, deadline)
            auth = json.loads((runner.PRIVATE_CODEX_HOME / "auth.json").read_text())
            assert set(auth) == {"auth_mode", "tokens"}
            assert set(auth["tokens"]) == {"access_token", "account_id"}
            assert runner.PRIVATE_CODEX_HOME.stat().st_mode & 0o077 == 0
            assert (runner.PRIVATE_CODEX_HOME / "auth.json").stat().st_mode & 0o077 == 0
        harness = Harness(config)
        harness.openai_auth = runner.AccessOnlyAuth(credentials, tmp_path / "unused")
        children: list[Harness] = []
        try:
            await harness.initialize()
            child = harness.tasks._create_child("assistant")
            children.append(child)
            await child.initialize()
            for owner in (harness, child):
                provider = owner.agent.provider
                assert type(provider) is OpenAIProvider and provider.uses_chatgpt_auth
                assert provider.model == "gpt-6-astra"
                assert await provider._credentials() == credentials
                events = [event async for event in provider.generate([Message(role="user", content="offline probe")])]
                assert not any(isinstance(event, ErrorEvent) for event in events)
            assert harness.config.max_tool_rounds == 30 and child.config.max_tool_rounds == 12
            assert harness.config.shell_timeout == child.config.shell_timeout == 60
        finally:
            for child in children:
                await child.close()
            await harness.close()
    assert [item["deadline"] for item in captures] == [120, 120, 300, 300]
    requests = [{key: value for key, value in item.items() if key != "deadline"} for item in captures]
    assert all(item == requests[0] for item in requests)


@pytest.mark.parametrize("value", [False, True, 0, -1, "300", float("nan"), float("inf"), 10**400])
def test_job_and_agent_refuse_invalid_deadlines(tmp_path: Path, value: object) -> None:
    from benchmarks.terminal_bench.agent import NgnOptions

    with pytest.raises(ValueError):
        job_config(
            tmp_path, tmp_path / "creds", tmp_path / "jobs", 900, "gpt-6-astra", request_timeout=cast("float", value)
        )
    with pytest.raises(ValueError):
        NgnOptions(bundle=str(tmp_path), credentials_path=str(tmp_path / "creds"), request_timeout=value)


def test_older_runtime_keeps_callback_fixture_and_rejects_named_option_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(providers, "validate_request_timeout")
    config = runner.configuration(tmp_path, "gpt-6-astra", tmp_path / "private")
    if hasattr(config, "providers"):
        assert config.provider_profile().auth == "chatgpt"
    else:
        assert hasattr(config, "auth")
        assert config.auth == "chatgpt"
    with pytest.raises(ValueError, match=r"0\.18-compatible runtime"):
        runner.request_deadline(300)
    assert not runner.PRIVATE_CODEX_HOME.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["success", "constructor", "setup", "cancel_setup", "cancel_run", "residency", "account"]
)
async def test_private_access_files_cleaned_across_named_fixture_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "container_gate", lambda: None)
    credentials = synthetic_credentials()
    source = tmp_path / "credentials.json"
    payload = {
        "access_token": credentials.access_token,
        "account_id": credentials.account_id,
        "residency": credentials.residency,
    }
    if outcome in {"residency", "account"}:
        if outcome == "account":
            # Missing explicit account falls back to the token claim; refuse it.
            payload["account_id"] = ""
        else:
            payload["residency"] = "different-region"
    source.write_text(json.dumps(payload))
    source.chmod(0o600)
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("offline fixture")
    args = argparse.Namespace(
        output=str(tmp_path / "output"),
        credentials=str(source),
        instruction=str(instruction),
        model="gpt-6-astra",
        timeout=5,
        request_timeout=300,
    )

    if outcome == "constructor":

        def fail_constructor(config: object) -> Harness:
            assert (runner.PRIVATE_CODEX_HOME / "auth.json").exists()
            raise RuntimeError("synthetic constructor failure")

        monkeypatch.setattr(runner, "Harness", fail_constructor)
    elif outcome in {"setup", "cancel_setup"}:

        async def fail_setup(self: Harness, *, create_session: bool = True) -> None:
            if outcome == "cancel_setup":
                raise asyncio.CancelledError
            raise RuntimeError("synthetic setup failure")

        monkeypatch.setattr(Harness, "initialize", fail_setup)

    async def generate(self: OpenAIProvider, *args: object, **kwargs: object) -> AsyncIterator[Event]:
        if outcome == "cancel_run":
            raise asyncio.CancelledError
        yield TextDoneEvent(text="offline completion")

    monkeypatch.setattr(OpenAIProvider, "generate", generate)
    if outcome.startswith("cancel_"):
        with pytest.raises(asyncio.CancelledError):
            await runner.run(args)
    else:
        assert await runner.run(args) == (0 if outcome == "success" else 1)
    assert not source.exists()
    assert not runner.PRIVATE_CODEX_HOME.exists()
    records = (tmp_path / "output/events.jsonl").read_text()
    assert credentials.access_token not in records
    assert "different-region" not in records
    if outcome in {"constructor", "residency", "account"}:
        assert '"event": "benchmark_start"' not in records


def test_reused_runtime_is_copied_unchanged_and_tampering_is_rejected(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[3]
    original = tmp_path / "original"
    original.mkdir()
    wheel = original / "nagents-0.18.0-py3-none-any.whl"
    wheel.write_bytes(b"frozen-runtime-test-bytes")
    constraints = original / "runtime-constraints.txt"
    constraints.write_text("aiohttp==3.14.3\n")
    manifest = {
        "git_head": "frozen-runtime-commit",
        "git_dirty": False,
        "wheel": {"filename": wheel.name, "sha256": digest(wheel)},
        "runtime_constraints_sha256": digest(constraints),
        "harbor": {"version": HARBOR_VERSION, "commit": HARBOR_COMMIT},
        "dataset": {"version": "4.0.0", "repository": DATASET_REPO, "commit": DATASET_COMMIT},
        "source_files_sha256": {"src/nagents/example.py": "frozen-source-hash"},
    }
    provenance = original / "provenance.json"
    provenance.write_text(json.dumps(manifest))
    before = provenance.read_bytes()
    output = tmp_path / "comparison"
    reused = reuse_runtime_bundle(repo, output, original, ("data-anonymization",))
    assert (output / wheel.name).read_bytes() == wheel.read_bytes()
    assert (output / constraints.name).read_bytes() == constraints.read_bytes()
    assert reused["git_head"] == manifest["git_head"]
    assert reused["source_files_sha256"] == manifest["source_files_sha256"]
    assert reused["runner_sha256"] == digest(repo / "benchmarks/terminal_bench/runner.py")
    assert reused["adapter_git_head"]
    assert provenance.read_bytes() == before
    wheel.write_bytes(b"changed-runtime")
    with pytest.raises(ValueError, match="digest"):
        reuse_runtime_bundle(repo, tmp_path / "rejected", original)
    assert not (tmp_path / "rejected").exists()
