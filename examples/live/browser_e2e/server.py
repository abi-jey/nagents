"""Isolated browser E2E server; normal ngn app with a test-only capture asset."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import struct
import subprocess
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import WebSocket
from starlette.responses import FileResponse
from starlette.websockets import WebSocketDisconnect

import nagents.web.http.app as web_app
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.web.http.app import create_app
from nagents.web.http.runtime import server_logging_config

if TYPE_CHECKING:
    from fastapi import FastAPI

SESSION = "voice-e2e-offline"
REVISION = "e" * 64


def log_configuration() -> dict[str, object]:
    """Use the application logging allowlist with UTC millisecond timestamps."""
    return server_logging_config()


class ScriptedVoice:
    """Offline browser/HTTP/WebSocket/worklet test, without a model or delegation."""

    def __init__(self, fixtures: Path, gap_ms: int) -> None:
        with wave.open(str(fixtures / "reply.wav"), "rb") as source:
            if source.getframerate() != 24000 or source.getsampwidth() != 2 or source.getnchannels() != 1:
                raise ValueError("reply.wav must be mono PCM16 at 24000 Hz")
            self.reply = source.readframes(source.getnframes())
        manifest = json.loads((fixtures / "fixture.json").read_text())
        self.caller_text = str(manifest["caller_text"])
        self.reply_text = str(manifest["reply_text"])
        self.gap_ms = gap_ms
        self.status = "closed"
        self.events: list[dict[str, object]] = []
        self.stop = asyncio.Event()
        self.trace_path = fixtures / "server-events.jsonl"

    def log(self, kind: str, **values: object) -> None:
        with self.trace_path.open("a") as target:
            target.write(
                json.dumps({"type": kind, "monotonicNs": time.monotonic_ns(), "wallNs": time.time_ns(), **values})
                + "\n"
            )

    def event(self, speaker: str, text: str) -> None:
        self.events.append({"seq": len(self.events) + 1, "type": "transcript", "speaker": speaker, "text": text})
        self.log("scripted.caption", speaker=speaker, text=text)

    def snapshot(self, after: int = 0) -> dict[str, object]:
        return {
            "session_id": SESSION,
            "status": self.status,
            "model": "scripted-voice-fixture",
            "voice": "cove",
            "events": self.events[after:],
            "cursor": len(self.events),
        }

    def register(self, router: APIRouter) -> None:
        @router.get("/api/live")
        async def capability() -> dict[str, object]:
            return {
                "available": True,
                "reason": "",
                "provider": "openai",
                "revision": REVISION,
                "enabled": True,
                "key_configured": True,
                "model": "scripted-voice-fixture",
                "backend_model": "offline",
                "backend_mode": "assistant",
                "assistant": {"provider": "fixture", "model": "offline", "agent": "assistant"},
                "voice": "cove",
                "voices": ["cove"],
                "active_session_id": SESSION if self.status == "connected" else "",
                "transport": "websocket",
                "voice_auth": "chatgpt",
                "context_mode": "none",
            }

        @router.post("/api/live/sessions", status_code=201)
        async def create() -> dict[str, object]:
            if self.status == "connected":
                raise HTTPException(409, "Close the existing fixture session first")
            self.status = "connected"
            self.events.clear()
            self.stop.clear()
            self.log("session.created", gapMs=self.gap_ms)
            return {"session_id": SESSION, "model": "scripted-voice-fixture", "voice": "cove"}

        @router.get("/api/live/sessions/{session_id}")
        async def snapshot(session_id: str, after: int = 0) -> dict[str, object]:
            if session_id != SESSION:
                raise HTTPException(404)
            return self.snapshot(after)

        @router.post("/api/live/sessions/{session_id}/close")
        async def close(session_id: str) -> dict[str, object]:
            if session_id != SESSION:
                raise HTTPException(404)
            self.stop.set()
            self.status = "closed"
            self.log("session.closed")
            return self.snapshot()

        @router.websocket("/api/live/sessions/{session_id}/audio")
        async def audio(socket: WebSocket, session_id: str) -> None:
            if session_id != SESSION or self.status != "connected":
                await socket.close(code=1008)
                return
            await socket.accept(subprotocol="ngn.live.v2")
            received = asyncio.Event()

            async def receive() -> None:
                voiced = 0
                quiet = 0
                total = 0
                while not self.stop.is_set():
                    data = await socket.receive_bytes()
                    if not data or len(data) % 2 or len(data) > 48000:
                        raise ValueError("Invalid browser PCM frame")
                    samples = [sample[0] for sample in struct.iter_unpack("<h", data)]
                    rms = (sum(sample * sample for sample in samples) / len(samples)) ** 0.5 / 32768
                    total += len(samples)
                    if rms > 0.01:
                        voiced += len(samples)
                        quiet = 0
                    else:
                        quiet += len(samples)
                    self.log("microphone.frame", samples=len(samples), totalSamples=total, rms=rms)
                    # Signal detection only: captions are explicitly scripted,
                    # and this does not claim to recognize the spoken phrase.
                    if voiced >= 12000 and quiet >= 24000:
                        received.set()

            async def send() -> None:
                await received.wait()
                self.event("user", self.caller_text)
                await asyncio.sleep(0.25)
                self.event("assistant", self.reply_text)
                started = asyncio.get_running_loop().time()
                delayed = 0.0
                for offset in range(0, len(self.reply), 960):
                    if self.stop.is_set():
                        return
                    if self.gap_ms and offset == 48000:
                        delayed = self.gap_ms / 1000
                        self.log("fixture.injected_gap", milliseconds=self.gap_ms, sample=offset // 2)
                    due = started + offset / 48000 + delayed
                    await asyncio.sleep(max(0, due - asyncio.get_running_loop().time()))
                    frame = self.reply[offset : offset + 960]
                    await socket.send_bytes(frame)
                    self.log("speaker.frame", samples=len(frame) // 2, sourceSample=offset // 2)
                self.log("fixture.reply_complete", samples=len(self.reply) // 2)
                await self.stop.wait()

            receiver = asyncio.create_task(receive())
            sender = asyncio.create_task(send())
            stopper = asyncio.create_task(self.stop.wait())
            try:
                done, _ = await asyncio.wait({receiver, sender, stopper}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                for task in (receiver, sender, stopper):
                    task.cancel()
                await asyncio.gather(receiver, sender, stopper, return_exceptions=True)
                self.status = "closed"
                self.log("socket.closed")


def instrument(app: FastAPI, fixtures: Path, *, offline: bool, gap_ms: int) -> None:
    router = APIRouter()

    @router.get("/assets/voice-e2e-capture.js")
    async def recorder() -> FileResponse:
        return FileResponse(Path(__file__).with_name("capture-worklet.js"), media_type="text/javascript")

    @router.get("/__voice-e2e/meta")
    async def metadata() -> dict[str, object]:
        source_root = Path(web_app.__file__).resolve().parent.parent
        package_root = source_root.parents[1]
        module_names = (
            "web/http/app.py",
            "web/live/routes.py",
            "web/live/runtime.py",
            "web/live/relay.py",
            "web/live/updates.py",
        )
        modules = {
            name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
            for name in module_names
            if (source_root / name).is_file()
        }
        revision = ""
        dirty: bool | None = None
        try:
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=package_root, check=True, capture_output=True, text=True
            ).stdout.strip()
            dirty = bool(
                subprocess.run(
                    ["git", "status", "--porcelain"], cwd=package_root, check=True, capture_output=True, text=True
                ).stdout.strip()
            )
        except (OSError, subprocess.CalledProcessError):
            pass
        try:
            version = importlib.metadata.version("nagents")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        return {
            "packageVersion": version,
            "sourceRevision": revision or "unavailable",
            "sourceTreeDirty": dirty,
            "moduleSha256": modules,
        }

    @router.get("/assets/voice-e2e-input.wav")
    async def caller() -> FileResponse:
        fixture = fixtures / "round-1.wav"
        return FileResponse(fixture if fixture.is_file() else fixtures / "caller.wav", media_type="audio/wav")

    @router.get("/assets/voice-e2e-input-round-{round_number}.wav")
    async def caller_round(round_number: int) -> FileResponse:
        if not 1 <= round_number <= 64:
            raise HTTPException(404)
        fixture = fixtures / f"round-{round_number}.wav"
        return FileResponse(fixture if fixture.is_file() else fixtures / "caller.wav", media_type="audio/wav")

    if offline:
        ScriptedVoice(fixtures, gap_ms).register(router)
    # Keep the normal app lifespan and LocalOnly token/origin checks. Only this
    # explicitly launched test process mounts the recorder/scripted routes.
    app.router.routes[0:0] = router.routes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--config", default="", help="Explicit isolated ngn JSON configuration")
    parser.add_argument(
        "--authenticated", action="store_true", help="Opt in to normal ngn providers, credentials, and delegated work"
    )
    parser.add_argument("--gap-ms", type=int, default=0, help="Offline: inject one playback network gap")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if args.authenticated:
        config = load_config(workspace, Path(args.config)) if args.config else load_config(workspace)
        if config.demo:
            raise SystemExit("Authenticated mode requires a non-demo workspace configuration")
        # Preserve normal provider/auth selection but never share session DBs
        # with the user's independently running ngn server.
        config.data_dir = workspace / "voice-e2e-state"
    else:
        config = HarnessConfig(workspace=workspace, data_dir=workspace / "data", demo=True)
    app = create_app(config, port=args.port)
    instrument(app, args.fixtures, offline=not args.authenticated, gap_ms=args.gap_ms)
    print(f"Voice E2E mode: {'authenticated GPT Live' if args.authenticated else 'offline scripted fixture'}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info", log_config=log_configuration())


if __name__ == "__main__":
    main()
