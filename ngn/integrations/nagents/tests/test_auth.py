from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from collections.abc import AsyncIterator
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.provider.openai import CodexCredentials
from nagents.tools import ToolRegistry
from nagents.types import GenerationConfig
from nagents.types import Message
from nagents.types import ToolDefinition

from ngn_nagents import auth
from ngn_nagents.protocol import Emitter
from ngn_nagents.protocol import ProtocolError
from ngn_nagents.runtime import HostExecutor
from ngn_nagents.runtime import Start
from ngn_nagents.runtime import _create_codex_provider
from ngn_nagents.runtime import create_agent
from ngn_nagents.runtime import create_provider
from ngn_nagents.runtime import run_agent


def subscription() -> dict[str, object]:
    return {
        "kind": "openai",
        "model": "test-model",
        "api": "responses",
        "base_url": "",
        "api_key_env": "",
        "auth": "chatgpt",
    }


def message(provider: dict[str, object], path: str) -> dict[str, object]:
    return {
        "type": "start",
        "run": {
            "session_id": "s",
            "prompt": "hello",
            "system_prompt": "policy",
            "database": "/runtime/nagents.sqlite3",
            "provider": provider,
            "tools": [],
            "max_tool_rounds": 10,
            "auth_store": path,
        },
    }


def queue_emitter(frames: asyncio.Queue[dict[str, object]]) -> Emitter:
    def emit(run_id: str, frame: dict[str, object]) -> None:
        frames.put_nowait(frame)

    return emit


@dataclass
class FakeAuthorization:
    verification_url: str = auth.VERIFICATION_URL
    user_code: str = "TEST-CODE"
    device_auth_id: str = "PRIVATE-DEVICE-ID"


class FakeAuth:
    def __init__(self) -> None:
        self.authorization = FakeAuthorization()
        self.closed = False
        self.logged_out = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()

    async def start_device_login(self) -> FakeAuthorization:
        return self.authorization

    async def complete_device_login(self, authorization: auth.Authorization) -> None:
        self.started.set()
        if self.block:
            await self.release.wait()

    async def credentials(self) -> CodexCredentials:
        raise AssertionError("Status/login must not resolve inference credentials")

    def status(self) -> str:
        return "Not signed in"

    async def logout_async(self) -> None:
        self.logged_out = True

    async def close(self) -> None:
        self.close_started.set()
        await self.close_release.wait()
        self.closed = True


class FakeAgent:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def run(self, prompt: str, *, session_id: str, user_id: str) -> AsyncIterator[object]:
        self.started.set()
        await asyncio.Event().wait()
        yield object()

    async def close(self) -> None:
        self.closed = True


class SteeringProvider(Provider):
    """Real Agent/plugin lifecycle without authentication or inference network IO."""

    def __init__(self) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "synthetic", "test-model")
        self.requests = 0

    async def verify_model(self, force: bool = False) -> bool:
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        self.requests += 1
        yield TextDoneEvent(text="offline final")


class AuthTests(unittest.IsolatedAsyncioTestCase):
    def private_store(self) -> str:
        return str(Path(self.enterContext(tempfile.TemporaryDirectory())) / "auth/openai.json")

    async def test_strict_subscription_wire_and_api_key_unchanged(self) -> None:
        start = Start.parse(message(subscription(), "/host/auth/openai.json"))
        self.assertEqual(start.auth_store, Path("/host/auth/openai.json"))
        self.assertFalse(start.steering)
        for enabled in (False, True):
            wire = message(subscription(), "/host/auth/openai.json")
            run = wire["run"]
            assert isinstance(run, dict)
            run["steering"] = enabled
            parsed = Start.parse(wire)
            self.assertEqual(parsed.auth_store, start.auth_store)
            self.assertIs(parsed.steering, enabled)
        for changes in (
            {"auth": "codex"},
            {"auth": ""},
            {"api_key_env": "OPENAI_API_KEY"},
            {"kind": "openai_compatible"},
            {"api": "chat_completions"},
            {"base_url": "https://example.invalid/v1"},
            {"base_url": "https://chatgpt.com/backend-api/codex/responses"},
            {"model": "x" * 129},
            {"model": "x/y"},
            {"model": "test\nmodel"},
            {"api_key": "PRIVATE-KEY"},
            {"credentials": "callback"},
        ):
            settings = subscription() | changes
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                Start.parse(message(settings, "/host/auth/openai.json"))
            with self.assertRaises(ProtocolError):
                create_provider(settings)
        for path in (
            "relative/auth/openai.json",
            "/host/../auth/openai.json",
            "/host/./auth/openai.json",
            "/host/auth.json",
            "",
        ):
            with self.subTest(path=path), self.assertRaises(ProtocolError):
                Start.parse(message(subscription(), path))
        api_key: dict[str, object] = {
            "kind": "openai_compatible",
            "model": "test",
            "api": "auto",
            "base_url": "",
            "api_key_env": "TEST_KEY",
        }
        wire = message(api_key, "/host/auth/openai.json")
        with self.assertRaises(ProtocolError):
            Start.parse(wire)
        run = wire["run"]
        assert isinstance(run, dict)
        del run["auth_store"]
        self.assertIsNone(Start.parse(wire).auth_store)
        with patch.dict(os.environ, {"TEST_KEY": "synthetic-key"}):
            provider = create_provider(api_key)
        self.assertEqual(provider.api_key, "synthetic-key")
        await provider.close()
        with (
            patch.dict(os.environ, {"TEST_KEY": "", "OPENAI_API_KEY": "must-not-fallback"}),
            self.assertRaises(ProtocolError),
        ):
            create_provider(api_key)

    async def test_missing_public_core_is_actionable_redacted_and_not_advertised(self) -> None:
        with patch("ngn_nagents.auth.importlib.import_module", side_effect=ImportError("PRIVATE-ERROR")):
            self.assertFalse(auth.available())
            with self.assertRaises(ProtocolError) as captured:
                auth.auth_factory()
            self.assertIn("0.28.5", str(captured.exception))
            self.assertIn("source-built", str(captured.exception))
            self.assertNotIn("PRIVATE", str(captured.exception))
            frames: list[dict[str, object]] = []
            with patch.object(auth, "output", side_effect=frames.append):
                self.assertEqual(await auth.serve(["status", "--store", "/host/auth/openai.json"]), 2)
            self.assertEqual(frames, [{"type": "error", "code": "missing_core"}])

    async def test_login_frames_exclude_private_authorization_and_tokens(self) -> None:
        fake = FakeAuth()
        frames: list[dict[str, object]] = []
        with patch("ngn_nagents.auth.auth_factory", return_value=lambda path: fake) as factory:
            await auth.operate("login", Path("/host/auth/openai.json"), frames.append)
        factory.assert_called_once_with()
        self.assertEqual(
            frames,
            [
                {"type": "challenge", "verification_url": auth.VERIFICATION_URL, "user_code": "TEST-CODE"},
                {"type": "status", "status": "signed_in"},
            ],
        )
        self.assertTrue(fake.closed)
        self.assertNotIn("PRIVATE-DEVICE-ID", json.dumps(frames))
        for name in ("device_auth_id", "access_token", "refresh_token", "id_token", "body", "error"):
            with (
                self.subTest(name=name),
                self.assertRaises(ProtocolError) as captured,
                redirect_stdout(io.StringIO()) as stream,
            ):
                auth.output({"type": "status", "status": "signed_in", name: "PRIVATE-SECRET"})
            self.assertEqual(stream.getvalue(), "")
            self.assertNotIn("PRIVATE-SECRET", str(captured.exception))
        invalid_frames: tuple[dict[str, object], ...] = (
            {"type": "error", "code": "PRIVATE-SECRET"},
            {"type": "status", "status": ["PRIVATE-SECRET"]},
            {"type": "challenge", "verification_url": "https://evil.invalid", "user_code": "TEST"},
            {"type": "challenge", "verification_url": auth.VERIFICATION_URL, "user_code": "\x1b[31m"},
            {"type": "challenge", "verification_url": auth.VERIFICATION_URL, "user_code": "x" * 65},
        )
        for invalid in invalid_frames:
            with self.assertRaises(ProtocolError), redirect_stdout(io.StringIO()):
                auth.output(invalid)

    async def test_unknown_errors_and_arguments_are_redacted(self) -> None:
        frames: list[dict[str, object]] = []
        with (
            patch("ngn_nagents.auth.output", side_effect=frames.append),
            patch("ngn_nagents.auth.operate", side_effect=RuntimeError("PRIVATE-SECRET")),
        ):
            self.assertEqual(await auth.serve(["status", "--store", "/host/auth/openai.json"]), 2)
        self.assertEqual(frames, [{"type": "error", "code": "auth_failed"}])
        for arguments in (
            [],
            ["login"],
            ["import", "--store", "/host/auth/openai.json"],
            ["status", "--store", "relative"],
            ["status", "--store", "/host/auth/openai.json", "PRIVATE-SECRET"],
        ):
            frames.clear()
            with patch("ngn_nagents.auth.output", side_effect=frames.append):
                self.assertEqual(await auth.serve(arguments), 2)
            self.assertEqual(frames, [{"type": "error", "code": "invalid_request"}])

    async def test_auth_cancellation_joins_its_cleanup_even_if_cancelled_again(self) -> None:
        fake = FakeAuth()
        fake.block = True
        fake.close_release.clear()
        frames: list[dict[str, object]] = []
        with patch("ngn_nagents.auth.auth_factory", return_value=lambda path: fake):
            task = asyncio.create_task(auth.operate("login", Path("/host/auth/openai.json"), frames.append))
            await asyncio.wait_for(fake.started.wait(), 2)
            task.cancel()
            await asyncio.wait_for(fake.close_started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            fake.close_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(fake.closed)
        self.assertEqual(len(frames), 1)

    async def test_runtime_explicit_callback_and_auth_closed_on_construction_error(self) -> None:
        fake = FakeAuth()
        provider = Provider(ProviderType.OPENAI_COMPATIBLE, api_key="synthetic", model="test")
        close = AsyncMock()
        frames: list[dict[str, object]] = []
        executor = HostExecutor(ToolRegistry(), "r", lambda run, frame: frames.append(frame))
        start = Start.parse(message(subscription(), self.private_store()))
        with (
            patch("ngn_nagents.runtime.auth_factory", return_value=lambda path: fake),
            patch("nagents.provider.openai.OpenAIProvider", return_value=provider) as constructor,
            patch.object(provider, "close", close),
            patch("ngn_nagents.runtime.create_agent", side_effect=RuntimeError("PRIVATE-ERROR")),
        ):
            await run_agent(start, executor, lambda run, frame: frames.append(frame))
        self.assertEqual(constructor.call_args.kwargs, {"credentials": fake.credentials, "model": "test-model"})
        close.assert_awaited_once()
        self.assertTrue(fake.closed)
        self.assertEqual(frames[-1]["status"], "failed")
        self.assertNotIn("PRIVATE-ERROR", json.dumps(frames))
        self.assertFalse(
            any(name.startswith(("nagents.harness", "nagents.tui", "nagents.web")) for name in sys.modules)
        )

    async def test_runtime_auth_closed_when_provider_construction_fails(self) -> None:
        fake = FakeAuth()
        frames: list[dict[str, object]] = []
        executor = HostExecutor(ToolRegistry(), "r", lambda run, frame: frames.append(frame))
        with (
            patch("ngn_nagents.runtime.auth_factory", return_value=lambda path: fake),
            patch("nagents.provider.openai.OpenAIProvider", side_effect=RuntimeError("PRIVATE-ERROR")),
        ):
            await run_agent(
                Start.parse(message(subscription(), self.private_store())),
                executor,
                lambda run, frame: frames.append(frame),
            )
        self.assertTrue(fake.closed)
        self.assertNotIn("PRIVATE-ERROR", json.dumps(frames))

    async def test_runtime_cancellation_joins_provider_and_auth(self) -> None:
        fake = FakeAuth()
        fake.close_release.clear()
        agent = FakeAgent()
        frames: list[dict[str, object]] = []
        executor = HostExecutor(ToolRegistry(), "r", lambda run, frame: frames.append(frame))
        provider = Provider(ProviderType.OPENAI_COMPATIBLE, api_key="synthetic", model="test")
        start = Start.parse(message(subscription(), self.private_store()))
        with (
            patch("ngn_nagents.runtime.auth_factory", return_value=lambda path: fake),
            patch("nagents.provider.openai.OpenAIProvider", return_value=provider),
            patch("ngn_nagents.runtime.create_agent", return_value=agent),
        ):
            task = asyncio.create_task(
                run_agent(
                    start,
                    executor,
                    lambda run, frame: frames.append(frame),
                )
            )
            await asyncio.wait_for(agent.started.wait(), 2)
            task.cancel()
            await asyncio.wait_for(fake.close_started.wait(), 2)
            assert start.auth_store is not None
            with self.assertRaises(auth.AuthFailure), auth.mutation_guard(start.auth_store):
                self.fail("Runtime identity guard released before provider/auth cleanup")
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            fake.close_release.set()
            await task
        self.assertTrue(fake.closed and agent.closed)
        self.assertEqual(frames[-1], {"type": "finished", "status": "cancelled", "error": ""})
        assert start.auth_store is not None
        with auth.mutation_guard(start.auth_store):
            pass
        await provider.close()

    @unittest.skipUnless(os.name == "posix", "POSIX identity ownership")
    async def test_subscription_steering_retains_one_agent_callback_and_guard_through_cleanup(self) -> None:
        from uuid import uuid4

        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                wire = message(subscription(), str(base / "auth/openai.json"))
                run = wire["run"]
                assert isinstance(run, dict)
                run.update(database=str(base / "runtime.sqlite3"), steering=True)
                start = Start.parse(wire)
                assert start.auth_store is not None
                fake = FakeAuth()
                fake.close_release.clear()
                provider = SteeringProvider()
                close = AsyncMock(wraps=provider.close)
                messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                executor = HostExecutor(ToolRegistry(), "subscription-run", queue_emitter(messages))
                with (
                    patch(
                        "ngn_nagents.runtime.auth_factory", return_value=lambda path, fake=fake: fake
                    ) as auth_factory,
                    patch("nagents.provider.openai.OpenAIProvider", return_value=provider) as constructor,
                    patch("ngn_nagents.runtime.create_agent", wraps=create_agent) as agents,
                    patch.object(provider, "close", close),
                ):
                    task = asyncio.create_task(run_agent(start, executor, executor.emit))
                    boundaries = 0
                    applied = 0
                    try:
                        while boundaries < 2:
                            frame = await asyncio.wait_for(messages.get(), 10)
                            self.assertNotEqual(frame["type"], "finished", frame)
                            if frame["type"] == "steering_applied":
                                applied += 1
                                history = await SessionManager(start.database).get_history(start.session_id)
                                self.assertEqual(
                                    [item.content for item in history if item.role == "user"],
                                    ["hello", "continue explicitly"],
                                )
                            if frame["type"] != "steering_boundary":
                                continue
                            boundaries += 1
                            self.assertEqual(frame["kind"], "final")
                            self.assertFalse(fake.closed)
                            close.assert_not_awaited()
                            with self.assertRaises(auth.AuthFailure), auth.mutation_guard(start.auth_store):
                                self.fail("Final continuation released its authentication identity")
                            if boundaries == 1:
                                executor.steering.accept(
                                    {
                                        "type": "steering_offer",
                                        "boundary_id": frame["boundary_id"],
                                        "input": {
                                            "input_id": "steer-" + uuid4().hex,
                                            "request_id": str(uuid4()),
                                            "position": 1,
                                            "prompt": "continue explicitly",
                                        },
                                    }
                                )
                            elif cancel:
                                task.cancel()
                            else:
                                executor.steering.accept(
                                    {"type": "steering_offer", "boundary_id": frame["boundary_id"], "input": None}
                                )
                        await asyncio.wait_for(fake.close_started.wait(), 10)
                        with self.assertRaises(auth.AuthFailure), auth.mutation_guard(start.auth_store):
                            self.fail("Identity guard released before joined auth cleanup")
                        if cancel:
                            task.cancel()
                            await asyncio.sleep(0)
                            self.assertFalse(task.done())
                    finally:
                        fake.close_release.set()
                        await asyncio.wait_for(task, 10)
                    terminal = await asyncio.wait_for(messages.get(), 10)
                    self.assertEqual(
                        terminal,
                        {"type": "finished", "status": "cancelled" if cancel else "completed", "error": ""},
                    )
                    auth_factory.assert_called_once_with()
                    constructor.assert_called_once_with(credentials=fake.credentials, model="test-model")
                    agents.assert_called_once()
                    self.assertIs(agents.call_args.kwargs["provider"], provider)
                    self.assertIsNotNone(agents.call_args.kwargs["plugin"])
                    close.assert_awaited_once()
                self.assertEqual((boundaries, applied, provider.requests), (2, 1, 2))
                self.assertTrue(fake.closed)
                with auth.mutation_guard(start.auth_store):
                    pass

    async def test_real_status_is_network_free_and_logout_only_touches_explicit_store(self) -> None:
        if not auth.available():
            self.skipTest("Public device-auth API is unavailable in the published core")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            explicit = base / "native" / "auth" / "openai.json"
            legacy = base / "ngn" / "auth" / "openai.json"
            legacy.parent.parent.mkdir(mode=0o700)
            legacy.parent.mkdir(mode=0o700)
            legacy.write_text("legacy sentinel")
            frames: list[dict[str, object]] = []
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": str(base), "CODEX_HOME": str(base / "codex")}),
                patch(
                    "nagents.provider.device_auth.OpenAIAuth._post",
                    side_effect=AssertionError("Status/logout cannot contact network"),
                ),
                patch(
                    "nagents.provider.device_auth.OpenAIAuth.credentials",
                    side_effect=AssertionError("Status cannot refresh"),
                ),
            ):
                await auth.operate("status", explicit, frames.append)
                self.assertFalse(explicit.exists())
                self.assertFalse(explicit.parent.exists())
                explicit.parent.parent.mkdir(mode=0o700)
                explicit.parent.mkdir(mode=0o700)
                explicit.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "access_token": "synthetic-expired",
                            "refresh_token": "synthetic-refresh",
                            "id_token": "synthetic-id",
                            "expires_at": 1,
                        }
                    )
                )
                explicit.chmod(0o600)
                await auth.operate("status", explicit, frames.append)
                with auth.mutation_guard(explicit):
                    await auth.operate("logout", explicit, frames.append)
                self.assertFalse(explicit.exists())
                self.assertTrue(explicit.with_name("openai.json.state.json").exists())
                self.assertEqual(legacy.read_text(), "legacy sentinel")
            self.assertEqual(
                frames,
                [
                    {"type": "status", "status": "signed_out"},
                    {"type": "status", "status": "signed_in"},
                    {"type": "status", "status": "signed_out"},
                ],
            )

    @unittest.skipUnless(os.name == "posix", "POSIX flock")
    async def test_mutations_exclude_hosts_and_each_other_but_status_remains_local(self) -> None:
        import fcntl

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "auth" / "openai.json"
            with auth.mutation_guard(path):
                with self.assertRaises(auth.AuthFailure) as captured, auth.mutation_guard(path):
                    self.fail("Second mutator acquired ownership")
                self.assertEqual(captured.exception.code, "busy")
            lock = path.parent.parent / ".identity" / "host.lock"
            with lock.open("rb") as host:
                fcntl.flock(host, fcntl.LOCK_SH | fcntl.LOCK_NB)
                frames: list[dict[str, object]] = []
                with (
                    patch("ngn_nagents.auth.output", side_effect=frames.append),
                    patch("ngn_nagents.auth.auth_factory") as factory,
                ):
                    self.assertEqual(await auth.serve(["logout", "--store", str(path)]), 2)
                self.assertEqual(frames, [{"type": "error", "code": "busy"}])
                factory.assert_not_called()
            with auth.mutation_guard(path):
                pass

    @unittest.skipUnless(os.name == "posix", "POSIX flock")
    async def test_writer_process_owns_guard_until_its_own_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "auth" / "openai.json"
            code = "from pathlib import Path\nfrom ngn_nagents.auth import mutation_guard\nimport sys\nwith mutation_guard(Path(sys.argv[1])):\n print('ready', flush=True)\n sys.stdin.read()\n"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                str(path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                assert process.stdout is not None
                self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b"ready\n")
                with self.assertRaises(auth.AuthFailure), auth.mutation_guard(path):
                    self.fail("Writer lost its independent process ownership")
            finally:
                process.terminate()
                await asyncio.wait_for(process.communicate(), 5)
            with auth.mutation_guard(path):
                pass

    @unittest.skipUnless(os.name == "posix", "POSIX file checks")
    async def test_ownership_metadata_rejects_symlinks_hardlinks_and_public_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            path = base / "auth" / "openai.json"
            with auth.mutation_guard(path):
                pass
            lock = base / ".identity" / "host.lock"
            lock.chmod(0o644)
            with self.assertRaises(auth.AuthFailure), auth.mutation_guard(path):
                self.fail("Public lock accepted")
            lock.chmod(0o600)
            os.link(lock, base / "linked")
            with self.assertRaises(auth.AuthFailure), auth.mutation_guard(path):
                self.fail("Hardlinked lock accepted")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            target.mkdir(mode=0o700)
            (base / ".identity").symlink_to(target, target_is_directory=True)
            with self.assertRaises(auth.AuthFailure), auth.mutation_guard(base / "auth/openai.json"):
                self.fail("Symlink ownership directory accepted")


class CodexAuthTests(unittest.IsolatedAsyncioTestCase):
    """Codex must be locally selected; its configured OAuth/API-key policy wins."""

    def codex(self) -> dict[str, object]:
        return subscription() | {"auth": "codex", "api": "auto"}

    def test_codex_wire_needs_no_host_store_or_host_api_key(self) -> None:
        wire = message(self.codex(), "/host/auth/openai.json")
        run = wire["run"]
        assert isinstance(run, dict)
        del run["auth_store"]
        self.assertIsNone(Start.parse(wire).auth_store)
        with self.assertRaises(ProtocolError):
            create_provider(self.codex())
        for changes in (
            {"api_key_env": "OPENAI_API_KEY"},
            {"kind": "anthropic"},
            {"base_url": "https://example.invalid/v1"},
            {"api": "chat_completions"},
        ):
            run["provider"] = self.codex() | changes
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                Start.parse(wire)

    async def test_codex_requires_local_configuration_before_using_any_key(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"CODEX_HOME": root, "OPENAI_API_KEY": "environment-fixture"}, clear=True),
        ):
            with self.assertRaisesRegex(ProtocolError, "sign in with Codex"):
                _create_codex_provider(self.codex())

            home = Path(root)
            (home / "config.toml").write_text('forced_login_method = "api"\n')
            provider = _create_codex_provider(self.codex())
            try:
                self.assertEqual(provider.api_key, "environment-fixture")
            finally:
                await provider.close()

            (home / "config.toml").unlink()
            (home / "auth.json").write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"cached-fixture"}')
            provider = _create_codex_provider(self.codex())
            try:
                self.assertEqual(provider.api_key, "cached-fixture")
            finally:
                await provider.close()

            (home / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{}}')
            with self.assertRaisesRegex(ProtocolError, "configuration are unusable"):
                _create_codex_provider(self.codex())


if __name__ == "__main__":
    unittest.main()
