"""Local-audio host for the web client's shared Live call owner and settings.

Loaded only on an explicit voice action. There is no listener, HTTP server,
browser, or second assistant: requests enter the existing TUI Harness.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING

from fastapi import HTTPException

from nagents._async import join_owned
from nagents.harness.runtime.core import _HarnessSession
from nagents.web.live.context import message_seed
from nagents.web.live.context import read_session_seed
from nagents.web.live.context import with_summary
from nagents.web.live.routes import create_agent
from nagents.web.live.routes import create_login_config
from nagents.web.live.routes import unavailable_reason
from nagents.web.live.runtime import LiveService
from nagents.web.live.settings import AgentVoiceInput
from nagents.web.live.settings import AgentVoiceRevision
from nagents.web.live.settings import GlobalVoiceInput
from nagents.web.live.settings import LiveSettings
from nagents.web.live.settings import WorkspaceVoiceInput
from nagents.web.live.summary import VoiceContextSummarizer
from nagents.web.voice.preferences import VoiceOverrides
from nagents.web.voice.preferences import VoicePreferences

from .voice_audio import AudioDeviceError
from .voice_audio import LocalAudio
from .voice_bridge import TuiVoiceBridge

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from nagents.agent import Agent
    from nagents.harness import Harness
    from nagents.live.delegation import ClientDelegationRequest
    from nagents.web.live.context import LiveSeed
    from nagents.web.live.login import LoginVoiceConfig

    from .voice import VoicePrompt


class TuiVoice:
    def __init__(
        self,
        harness: Harness,
        submit: Callable[[VoicePrompt], Awaitable[None]],
        changed: Callable[[dict[str, object], bool], None],
        run_id: Callable[[], str] = lambda: "",
    ) -> None:
        self.harness, self.submit, self.changed = harness, submit, changed
        self.run_id = run_id
        self.service = LiveService(self._agent, login_factory=self._login, context_factory=self._context)
        self.settings = LiveSettings(
            harness.agent.session.db_path,
            demo=harness.config.demo,
            active=lambda: self.service.active_session_id,
            providers=harness.provider_store,
            auth=harness.openai_auth,
            harness=harness,
            selected_agent=lambda: harness.config.agent,
        )
        self.bridges: deque[TuiVoiceBridge] = deque(maxlen=8)
        self._summary = VoiceContextSummarizer()
        self._loaded = False
        self._closed = False
        self._starting: asyncio.Task[object] | None = None
        self._monitor: asyncio.Task[None] | None = None
        self.audio: LocalAudio | None = None
        self.input_device = -1
        self.output_device = -1
        self.payload: dict[str, object] = {}
        self._stop_lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._starting is not None or bool(self.service.active_session_id)

    @property
    def muted(self) -> bool:
        return self.audio is not None and self.audio.input.muted

    def _changed(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.changed(payload, self.muted)

    async def load(self) -> None:
        if not self._loaded:
            await self.settings.load()
            self._loaded = True

    async def snapshot(self, scope: str = "agent") -> dict[str, object]:
        await self.load()
        if scope == "agent":
            return await self.settings.snapshot("agent", self.harness.config.agent)
        if scope in {"global", "workspace"}:
            return await self.settings.snapshot("global" if scope == "global" else "workspace")
        raise ValueError("Use /voice settings [agent|global|workspace]")

    async def save(self, snapshot: dict[str, object], changes: dict[str, object], *, reset: bool = False) -> None:
        scope, revision = snapshot["scope"], str(snapshot["revision"])
        if scope == "agent":
            agent = str(snapshot["agent"])
            if reset:
                await self.settings.change_agent(AgentVoiceRevision(scope="agent", agent=agent, revision=revision))
            else:
                await self.settings.change_agent(
                    AgentVoiceInput(scope="agent", agent=agent, revision=revision, overrides=changes)
                )
        elif scope == "global":
            await self.settings.change(
                GlobalVoiceInput(
                    scope="global", revision=revision, preferences=VoicePreferences.model_validate(changes)
                )
            )
        elif scope == "workspace":
            await self.settings.change(
                WorkspaceVoiceInput(
                    scope="workspace",
                    revision=revision,
                    overrides=VoiceOverrides.model_validate({} if reset else changes),
                )
            )

    def _bridge(self) -> TuiVoiceBridge:
        if not self.bridges:
            raise RuntimeError("The assistant voice bridge is unavailable")
        return self.bridges[-1]

    def _agent(self, voice: str) -> Agent:
        return create_agent(
            self.settings.admitted(),
            voice,
            client_request_handler=self._bridge().handle_request,
            client_request_observer=self._bridge().observe,
        )

    def _login(self, voice: str) -> LoginVoiceConfig | None:
        connection = self.settings.admitted()
        if connection.voice_auth != "chatgpt":
            return None

        # Login configuration is read before context_factory creates the bridge.
        # Defer its request handler/observer until the call is fully initialized.
        async def handle(request: ClientDelegationRequest) -> str:
            return await self._bridge().handle_request(request)

        def observe(request: ClientDelegationRequest) -> None:
            self._bridge().observe_native(request)

        return create_login_config(connection, voice, handle, observer=observe)

    async def _context(self, identifier: str) -> LiveSeed:
        connection = self.settings.admitted()
        root = self.settings.admitted_session()
        if root != self.harness.session_id:
            raise ValueError("The selected conversation changed before voice connected")
        bridge = TuiVoiceBridge(
            root,
            identifier,
            self.submit,
            self.service.delegation_reporter(identifier),
            agent=self.harness.config.agent,
            provider=self.harness.config.provider,
            model=self.harness.agent.provider.model,
        )
        self.bridges.append(bridge)
        if self.run_id():
            bridge.begin(self.run_id())
            bridge.turn(self.harness.followups.current)
        if connection.values.backend_mode == "assistant":
            self.service.bind_bridge(identifier, bridge)
        task_state = (
            "The assistant has work in progress; do not infer that it has finished." if self.harness._busy else ""
        )
        if type(self.harness.agent.session) is _HarnessSession:
            seed = await read_session_seed(
                self.harness.agent.session.db_path, root, connection.values.context_mode, task_state=task_state
            )
        else:
            seed = message_seed(
                await self.harness.history(), root, connection.values.context_mode, task_state=task_state
            )
        if connection.values.context_mode == "summary" and seed.summary_source:
            result = await self._summary.prepare(
                source=seed.summary_source,
                fingerprint=seed.fingerprint,
                session_id=root,
                provider=self.harness.agent.provider,
            )
            if result.text:
                seed = with_summary(seed, result.text)
        return seed

    async def start(self) -> None:
        if self._closed or self.active:
            return
        self._starting = asyncio.current_task()
        self._changed({"session_id": "", "status": "connecting", "message": "Opening voice…"})
        try:
            await self.load()
            connection = await self.settings.connection()
            reason = unavailable_reason(connection, demo=self.harness.config.demo)
            if reason:
                raise ValueError(reason.replace("ngn serve", "ngn").replace("server", "client"))
            async with self.settings.admit(connection.revision, self.harness.session_id):
                self.audio = LocalAudio(input_device=self.input_device, output_device=self.output_device)
                await self.audio.open()
                result = await self.service.create_duplex(self.audio.duplex)
            identifier = str(result["session_id"])
            self._changed(await self.service.snapshot(identifier))
            self._monitor = asyncio.create_task(self._watch(identifier, self.audio), name="ngn-tui-voice-monitor")
        except BaseException:
            if self.audio is not None:
                await self.audio.close()
            raise
        finally:
            self._starting = None

    async def _watch(self, identifier: str, audio: LocalAudio) -> None:
        try:
            while not self._closed:
                audio.check()
                payload = await self.service.snapshot(identifier)
                self._changed(payload)
                if payload["status"] in {"closed", "error"}:
                    return
                bridge = next((item for item in self.bridges if item.voice_session_id == identifier), None)
                if bridge is not None and bridge.failed:
                    raise AudioDeviceError(
                        "Assistant voice updates stopped. Results remain in the chat. Reconnect Voice."
                    )
                await asyncio.sleep(0.1)
        except Exception as error:
            await audio.quiet()
            result = await self.service.close(identifier)
            self._changed({**result, "status": "error", "message": error_message(error)})
        finally:
            await audio.close()

    def mute(self) -> None:
        if self.audio is None or not self.active:
            raise ValueError("Start voice before muting the microphone")
        self.audio.input.mute(not self.muted)
        self.changed(self.payload, self.muted)

    async def details(self) -> str:
        import json

        identifier = str(self.payload.get("session_id", ""))
        if not identifier:
            return "No voice call has started. Use /voice settings, then /voice start."
        snapshot = await self.service.snapshot(identifier)
        context = await self.service.context_details(identifier)
        return json.dumps({"call": snapshot, "startup_context": context}, ensure_ascii=False, indent=2)

    async def stop(self) -> None:
        async with self._stop_lock:
            identifier = self.service.active_session_id
            try:
                if self.audio is not None:
                    await self.audio.quiet()
                starting = self._starting
                if starting is not None and starting is not asyncio.current_task():
                    starting.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await join_owned(starting)
                identifier = self.service.active_session_id or identifier
                if identifier:
                    self._changed(await self.service.close(identifier))
                elif starting is not None:
                    self._changed({"session_id": "", "status": "closed", "message": "Voice setup cancelled."})
            finally:
                if self._monitor is not None:
                    self._monitor.cancel()
                    with suppress(asyncio.CancelledError):
                        await join_owned(self._monitor)
                    self._monitor = None
                if self.audio is not None:
                    await self.audio.close()

    async def shutdown(self) -> None:
        self._closed = True
        try:
            await self.stop()
        finally:
            try:
                await self.service.shutdown()
            finally:
                await self.settings.shutdown()
                self._summary.clear()


def error_message(error: Exception) -> str:
    if isinstance(error, HTTPException):
        return str(error.detail)
    if isinstance(error, ValueError):
        return str(error)
    if isinstance(error, AudioDeviceError):
        return str(error)
    return "Voice could not connect. Check /voice settings, provider sign-in, and /voice devices."
