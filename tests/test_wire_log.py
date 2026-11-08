"""Opt-in wire logging captures real transport payloads without auth headers."""

from __future__ import annotations

import asyncio
import json
from io import StringIO
from typing import TYPE_CHECKING

from aiohttp import web
from rich.console import Console

from nagents.harness.wire_log import WireLog
from nagents.observation import observe
from nagents.observation import observer
from nagents.tui.exit_summary import print_exit_summary

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


def test_wire_log_transport(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def respond(request: web.Request) -> web.Response:
            assert await request.json() == {"prompt": "private prompt"}
            return web.json_response({"answer": "private answer"}, headers={"Set-Cookie": "secret-cookie"})

        application = web.Application()
        application.router.add_post("/generate", respond)
        runner = web.AppRunner(application)
        await runner.setup()
        server = web.TCPSite(runner, "127.0.0.1", 0)
        await server.start()
        port = runner.addresses[0][1]
        wire = WireLog(tmp_path)
        try:
            import aiohttp

            from nagents.observation import trace_config

            async with aiohttp.ClientSession(trace_configs=[trace_config()]) as client:
                with wire.capture("session-one"):
                    async with client.post(
                        f"http://127.0.0.1:{port}/generate",
                        json={"prompt": "private prompt"},
                        headers={"Authorization": "Bearer credential", "X-Api-Key": "secret-api-key"},
                    ) as response:
                        await response.text()
            text = wire.path.read_text()
            entries = [json.loads(line) for line in text.splitlines()]
            assert {entry["event"] for entry in entries} >= {
                "http_request",
                "http_request_body",
                "http_response",
                "http_response_body",
            }
            assert all(entry["session_id"] == "session-one" for entry in entries)
            assert "private prompt" in text
            assert "private answer" in text
            assert "Bearer credential" not in text
            assert "secret-api-key" not in text
            assert "secret-cookie" not in text
            assert wire.path.stat().st_mode & 0o777 == 0o600
            assert observer.get() is None
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_wire_log_chains_observer_and_filters(tmp_path: Path) -> None:
    seen: list[str] = []
    token = observer.set(lambda kind, data: seen.append(kind))
    try:
        wire = WireLog(tmp_path)
        with wire.capture("one"):
            observe("model_start", content="not an HTTP event")
            observe("http_stream", data="stream payload")
        observe("http_stream", data="outside capture")
        assert seen == ["model_start", "http_stream", "http_stream"]
        assert "stream payload" in wire.path.read_text()
        assert "outside capture" not in wire.path.read_text()
        assert "model_start" not in wire.path.read_text()
    finally:
        observer.reset(token)


def test_harness_wire_capture_is_opt_in(tmp_path: Path) -> None:
    from contextlib import aclosing

    from nagents.harness import Harness
    from nagents.harness import HarnessConfig

    async def scenario() -> None:
        for enabled in (False, True):
            harness = Harness(
                HarnessConfig(
                    workspace=tmp_path,
                    data_dir=tmp_path / str(enabled),
                    demo=True,
                    wire_log=enabled,
                )
            )
            try:
                async with aclosing(harness.run("hello")) as events:
                    async for _event in events:
                        pass
                # The real generation iterator owns capture, not the caller.
                from nagents.events import TextChunkEvent

                async def generated(prompt: object) -> AsyncGenerator[TextChunkEvent, None]:
                    observe("http_request_body", body="harness payload")
                    yield TextChunkEvent(chunk="answer")

                from unittest.mock import patch

                with patch.object(harness, "_observed_generation_events", generated):
                    async with aclosing(harness._generation_events("probe")) as events:
                        async for _event in events:
                            pass
                assert (harness.wire_log is not None) == enabled
                if harness.wire_log is not None:
                    assert "harness payload" in harness.wire_log.path.read_text()
                else:
                    assert not list((tmp_path / str(enabled)).glob("logs/wire-*"))
                assert observer.get() is None
            finally:
                await harness.close()

    asyncio.run(scenario())


def test_nested_wire_capture_does_not_duplicate(tmp_path: Path) -> None:
    parent = WireLog(tmp_path)
    child = WireLog(tmp_path)
    seen: list[str] = []
    token = observer.set(lambda kind, data: seen.append(kind))
    try:
        with parent.capture("parent"):
            observe("http_stream", data="parent event")
            with child.capture("child"):
                observe("http_stream", data="child event")
            observe("http_stream", data="parent resumed")
        assert len(seen) == 3
        assert "child event" not in parent.path.read_text()
        entries = [json.loads(line) for line in child.path.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["session_id"] == "child"
    finally:
        observer.reset(token)


def test_wire_file_replacement_fails_closed(tmp_path: Path) -> None:
    for replacement in ("missing", "symlink", "file"):
        wire = WireLog(tmp_path)
        target = tmp_path / f"target-{replacement}"
        target.write_text("unchanged")
        wire.path.unlink()
        if replacement == "symlink":
            wire.path.symlink_to(target)
        elif replacement == "file":
            wire.path.write_text("replacement")
        with wire.capture("session"):
            observe("http_stream", data="sensitive payload")
        assert target.read_text() == "unchanged"
        if replacement == "missing":
            assert not wire.path.exists()
        elif replacement == "file":
            assert wire.path.read_text() == "replacement"


def test_exit_summary_reports_wire_log() -> None:
    output = StringIO()
    print_exit_summary(
        "1:00",
        "session",
        "ngn --resume session",
        "runtime.jsonl",
        opened=True,
        wire_log="wire.jsonl",
        console=Console(file=output, color_system=None),
    )
    assert "wire.jsonl" in output.getvalue()
    assert "Sensitive request/response" in output.getvalue()
    assert "Wire log off" not in output.getvalue()
