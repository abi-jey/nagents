"""Exercise the actual HTTP adapter, agent loop, harness, and CLI together."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import cast

import pytest
from aiohttp import web

from nagents.events import DoneEvent
from nagents.events import ToolResultEvent
from nagents.extensions import AgentPlugin
from nagents.extensions import ModelRequest
from nagents.extensions import RunContext
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.types import Message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.harness.types import ApprovalRequest


# Every scenario initializes the real harness and its guarded workspace tools.
pytestmark = pytest.mark.requires_posix


@asynccontextmanager
async def local_provider(*, change: bool = False) -> AsyncIterator[tuple[str, list[dict[str, object]]]]:
    requests: list[dict[str, object]] = []

    async def models(request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": "ngn-integration", "object": "model"}]})

    async def model(request: web.Request) -> web.Response:
        return web.json_response({"id": "ngn-integration", "object": "model"})

    async def chat(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer ngn-test-key"
        body: object = await request.json()
        assert isinstance(body, dict)
        payload = cast("dict[str, object]", body)
        requests.append(payload)
        messages = payload["messages"]
        has_result = isinstance(messages, list) and any(
            isinstance(message, dict) and message.get("role") == "tool" for message in messages
        )
        deltas: list[dict[str, object]]
        if has_result:
            deltas = [{"content": "The file "}, {"content": "contains seed content."}]
            reason = "stop"
        else:
            deltas = [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "read-1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": ""},
                        }
                    ]
                },
                {"tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}]},
                {"tool_calls": [{"index": 0, "function": {"arguments": '"sample.txt"}'}}]},
            ]
            if change:
                deltas.append(
                    {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "edit-2",
                                "type": "function",
                                "function": {
                                    "name": "edit",
                                    "arguments": json.dumps(
                                        {"path": "sample.txt", "old": "seed content", "new": "updated content"}
                                    ),
                                },
                            }
                        ]
                    }
                )
            reason = "tool_calls"
        chunks: list[dict[str, object]] = [
            {"id": "response-1", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas
        ]
        chunks.append(
            {
                "id": "response-1",
                "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140},
            }
        )
        return web.Response(
            text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n",
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_get("/v1/models/{model}", model)
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}/v1", requests
    finally:
        await runner.cleanup()


class RequestContext(AgentPlugin):
    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        return replace(request, messages=[Message(role="system", content="ephemeral-test-context"), *request.messages])


def test_real_adapter_harness_and_ephemeral_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NGN_TEST_API_KEY", "ngn-test-key")
    (tmp_path / "sample.txt").write_text("seed content\n")

    async def scenario() -> None:
        async with local_provider() as (url, requests):
            harness = Harness(
                HarnessConfig(
                    workspace=tmp_path,
                    data_dir=tmp_path / "state",
                    model="ngn-integration",
                    base_url=url,
                    api_key_env="NGN_TEST_API_KEY",
                )
            )
            try:
                await harness.initialize()
                harness.agent.plugins.append(RequestContext())
                events = [event async for event in harness.run("Read sample.txt")]
                assert any(isinstance(event, ToolResultEvent) and not event.error for event in events)
                assert any(isinstance(event, DoneEvent) and "seed content" in event.final_text for event in events)
                assert len(requests) == 2
                assert all("ephemeral-test-context" in json.dumps(request) for request in requests)
                assert all("_save_to" not in json.dumps(request.get("tools", [])) for request in requests)
                history = await harness.history()
                assert not any(message.content == "ephemeral-test-context" for message in history)
                assert any(message.role == "tool" and "seed content" in str(message.content) for message in history)
            finally:
                await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("allowed", [False, True])
def test_live_edit_is_bound_to_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed: bool) -> None:
    monkeypatch.setenv("NGN_TEST_API_KEY", "ngn-test-key")
    target = tmp_path / "sample.txt"
    target.write_text("seed content\n")

    async def scenario() -> None:
        async with local_provider(change=True) as (url, requests):
            harness = Harness(
                HarnessConfig(
                    workspace=tmp_path,
                    data_dir=tmp_path / "state",
                    model="ngn-integration",
                    base_url=url,
                    api_key_env="NGN_TEST_API_KEY",
                )
            )
            approvals: list[ApprovalRequest] = []

            async def approve(request: ApprovalRequest) -> bool:
                approvals.append(request)
                assert target.read_text() == "seed content\n"
                assert "-seed content" in request.preview
                assert "+updated content" in request.preview
                return allowed

            harness.approval_handler = approve
            try:
                events = [event async for event in harness.run("Make the approved small edit")]
                assert len(requests) == 2
                assert len(approvals) == 1
                assert "-seed content" in approvals[0].preview
                assert "+updated content" in approvals[0].preview
                result = next(event for event in events if isinstance(event, ToolResultEvent) and event.name == "edit")
                assert bool(result.error) is not allowed
                assert target.read_text() == ("updated content\n" if allowed else "seed content\n")
            finally:
                await harness.close()

    asyncio.run(scenario())


def test_cli_live_protocol_stream(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("seed content\n")

    async def scenario() -> None:
        async with local_provider() as (url, requests):
            env = {
                **os.environ,
                "NGN_TEST_API_KEY": "ngn-test-key",
                "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_DATA_HOME": str(tmp_path / "state"),
            }
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "nagents.cli",
                "run",
                "--json",
                "--workspace",
                str(tmp_path),
                "--provider",
                "openai",
                "--model",
                "ngn-integration",
                "--base-url",
                url,
                "--api-key-env",
                "NGN_TEST_API_KEY",
                "Read sample.txt",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            assert process.returncode == 0, stderr.decode()
            records = [json.loads(line) for line in stdout.decode().splitlines()]
            assert any(record["event"] == "tool_result" for record in records)
            assert records[-1]["event"] == "done"
            assert len(requests) == 2

    asyncio.run(scenario())
