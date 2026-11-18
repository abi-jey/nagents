"""Optional installed-native boundary checks, with synthetic OAuth only.

NGN_TEST_BINARY must select this worktree's debug binary explicitly. No network
request or production credential read is made by the fixture runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

from ngn_nagents.auth import available

FIXTURE = """
import asyncio
import os
from pathlib import Path
from unittest.mock import patch
from nagents.provider.device_auth import OpenAIAuth
from ngn_nagents.__main__ import main

async def post(self, route, payload, deadline, *, form=False):
    if route == "/api/accounts/deviceauth/usercode":
        return 200, {"user_code": "TEST-CODE", "device_auth_id": "PRIVATE-DEVICE-ID", "interval": 1}, 1
    if route == "/api/accounts/deviceauth/token":
        if os.environ.get("NGN_SYNTHETIC_AUTH_HOLD"):
            Path(os.environ["NGN_SYNTHETIC_AUTH_HOLD"]).write_text(str(os.getpid()))
            await asyncio.Event().wait()
        return 200, {"authorization_code": "PRIVATE-AUTHORIZATION", "code_verifier": "PRIVATE-VERIFIER"}, 1
    if route == "/oauth/token":
        return 200, {"access_token": "PRIVATE-ACCESS", "refresh_token": "PRIVATE-REFRESH", "id_token": "PRIVATE-IDENTITY", "expires_in": 3600}, 1
    raise AssertionError("Unexpected OAuth endpoint")

with patch.object(OpenAIAuth, "_post", post):
    raise SystemExit(main())
"""


@unittest.skipUnless(os.environ.get("NGN_TEST_BINARY"), "Select an isolated debug binary with NGN_TEST_BINARY")
@unittest.skipUnless(os.name == "posix", "POSIX auth")
class NativeAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        if not available():
            self.skipTest("Source-built public device-auth API required for synthetic OAuth fixture")
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.binary = Path(os.environ["NGN_TEST_BINARY"]).resolve(strict=True)
        runner = self.base / "fixture.py"
        runner.write_text(FIXTURE)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.data = self.base / "native"
        self.store = self.data / "auth/openai.json"
        self.config = self.base / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "runner": [sys.executable, str(runner)],
                    "workspace": str(self.workspace),
                    "data_dir": str(self.data),
                    "provider": "s",
                    "providers": {"s": {"kind": "openai", "auth": "chatgpt", "model": "test-model"}},
                }
            )
        )
        self.environment = dict(os.environ)
        self.environment["CODEX_HOME"] = str(self.base / "must-not-discover")
        self.environment["XDG_DATA_HOME"] = str(self.base / "legacy-data")
        self.environment["OPENAI_API_KEY"] = "PRIVATE-API-KEY-MUST-NOT-FALLBACK"

    async def invoke(self, *arguments: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            str(self.binary),
            "--config",
            str(self.config),
            *arguments,
            env=self.environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        finally:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode is not None
        result = stdout.decode(), stderr.decode()
        self.assertNotIn("PRIVATE-", "".join(result))
        return process.returncode, *result

    async def test_synthetic_login_status_logout_through_native_command(self) -> None:
        code, stdout, _ = await self.invoke("auth", "status")
        self.assertEqual(code, 0)
        self.assertIn("Not signed in", stdout)
        code, stdout, stderr = await self.invoke("auth", "login")
        self.assertEqual(code, 0)
        self.assertIn("login saved", stdout)
        self.assertIn("https://auth.openai.com/codex/device", stderr)
        self.assertIn("TEST-CODE", stderr)
        self.assertTrue(self.store.is_file())
        code, stdout, stderr = await self.invoke("auth", "status")
        self.assertEqual(code, 0)
        self.assertIn("local presence only", stdout)
        self.assertEqual(stderr, "")
        code, stdout, _ = await self.invoke("auth", "logout")
        self.assertEqual(code, 0)
        self.assertIn("Not signed in", stdout)
        self.assertFalse(self.store.exists())
        self.assertFalse((self.base / "legacy-data/ngn/auth/openai.json").exists())
        self.assertFalse((self.base / "must-not-discover").exists())
        self.assertFalse((self.data / "workspaces").exists())
        self.assertEqual((self.data / ".identity/host.lock").stat().st_size, 0)

    async def test_pending_login_blocks_hosts_and_native_cancel_joins_writer(self) -> None:
        marker = self.base / "writer.pid"
        self.environment["NGN_SYNTHETIC_AUTH_HOLD"] = str(marker)
        process = await asyncio.create_subprocess_exec(
            str(self.binary),
            "--config",
            str(self.config),
            "auth",
            "login",
            env=self.environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stderr is not None
            challenge = await asyncio.wait_for(process.stderr.readline(), 5)
            self.assertIn(b"TEST-CODE", challenge)
            code, _, stderr = await self.invoke("sessions")
            self.assertNotEqual(code, 0)
            self.assertIn("Stop all ngn hosts", stderr)
            process.send_signal(signal.SIGINT)
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 130)
            self.assertNotIn(b"PRIVATE-", stdout_bytes + stderr_bytes)
            if marker.exists():
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(marker.read_text()), 0)
        finally:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.communicate(), 10)
        self.assertFalse(self.store.exists())
        code, stdout, _ = await self.invoke("sessions")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
