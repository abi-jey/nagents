from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from nagents.tools import ToolRegistry

from ngn_nagents import wire
from ngn_nagents.protocol import ProtocolError
from ngn_nagents.runtime import HostExecutor
from ngn_nagents.runtime import Start
from ngn_nagents.runtime import run_agent


@unittest.skipUnless(wire.available(), "POSIX wire logging and the public sink are required")
class WireTests(unittest.IsolatedAsyncioTestCase):
    def fixture_file(self, root: Path) -> tuple[Path, dict[str, str]]:
        directory = root / "logs"
        directory.mkdir(mode=0o700)
        path = directory / ("wire-" + "0" * 32 + ".jsonl")
        path.touch(mode=0o600, exist_ok=False)
        info = path.stat()
        return path, {wire.PATH_ENV: str(path), wire.IDENTITY_ENV: f"{info.st_dev}:{info.st_ino}"}

    def start_message(self, root: Path, base_url: str) -> dict[str, object]:
        return {
            "session_id": "wire-test",
            "prompt": "private prompt",
            "system_prompt": "private system prompt",
            "database": str(root / "runtime.sqlite3"),
            "provider": {
                "kind": "openai_compatible",
                "api": "chat_completions",
                "model": "wire-test",
                "base_url": base_url,
                "api_key_env": "FIXTURE_API_KEY",
            },
            "tools": [],
            "max_tool_rounds": 10,
        }

    async def test_private_local_provider_capture_requires_start_ack(self) -> None:
        async def models(_request: web.Request) -> web.Response:
            return web.json_response({"object": "list", "data": [{"id": "wire-test", "object": "model"}]})

        async def completion(request: web.Request) -> web.Response:
            self.assertEqual(request.headers["Authorization"], "Bearer local-test-credential")
            self.assertIn("private prompt", await request.text())
            chunks = (
                {
                    "id": "local",
                    "choices": [{"index": 0, "delta": {"content": "private answer"}, "finish_reason": None}],
                },
                {"id": "local", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            )
            return web.Response(
                text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n",
                content_type="text/event-stream",
                headers={"X-Private": "private response header", "Set-Cookie": "private cookie"},
            )

        app = web.Application()
        app.router.add_get("/v1/models", models)
        app.router.add_post("/v1/chat/completions", completion)
        server = web.AppRunner(app)
        await server.setup()
        try:
            site = web.TCPSite(server, "127.0.0.1", 0)
            await site.start()
            port = server.addresses[0][1]
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path, env = self.fixture_file(root)
                env["FIXTURE_API_KEY"] = "local-test-credential"
                run = self.start_message(root, f"http://127.0.0.1:{port}/v1")
                emitted: list[dict[str, object]] = []

                def emit(_run_id: str, message: dict[str, object]) -> None:
                    emitted.append(message)

                with patch.dict(os.environ, env):
                    for enabled in (False, True):
                        if enabled:
                            run["wire_log"] = True
                        start = Start.parse({"type": "start", "run": run})
                        emitted.clear()
                        executor = HostExecutor(ToolRegistry(), "synthetic", emit)
                        await run_agent(start, executor, emit)
                        self.assertEqual(emitted[-1], {"type": "finished", "status": "completed", "error": ""})
                        if not enabled:
                            self.assertEqual(path.read_text(), "")
                result = path.read_text()
                entries = [json.loads(line) for line in result.splitlines()]
                self.assertTrue({"http_request_body", "http_stream"} <= {entry["event"] for entry in entries})
                self.assertIn("private prompt", result)
                self.assertIn("private answer", result)
                self.assertNotIn("local-test-credential", result)
                self.assertNotIn("private response header", result)
                self.assertNotIn("private cookie", result)
        finally:
            await server.cleanup()

    def test_rejects_path_in_protocol_and_unsafe_host_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, env = self.fixture_file(root)
            run = self.start_message(root, "")
            for invalid in (False, None, 1, str(path)):
                with self.assertRaises(ProtocolError):
                    Start.parse({"type": "start", "run": {**run, "wire_log": invalid}})
            with patch.dict(os.environ, env):
                with patch.dict(os.environ, {wire.IDENTITY_ENV: "0:0"}), self.assertRaises(ProtocolError):
                    wire.open_host_log()
                path.chmod(0o644)
                with self.assertRaises(ProtocolError):
                    wire.open_host_log()
                path.chmod(0o600)
                moved = root / "original"
                path.rename(moved)
                os.link(moved, path)
                with self.assertRaises(ProtocolError):
                    wire.open_host_log()
                path.unlink()
                path.symlink_to(moved)
                with self.assertRaises(ProtocolError):
                    wire.open_host_log()
                self.assertEqual(moved.read_text(), "")
