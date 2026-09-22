"""Local browser media with native Live HTTP setup and an Agent-owned sideband."""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from collections import deque
from contextlib import aclosing
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.live._support import parser
from examples.live._support import voice
from nagents import AudioChunkEvent
from nagents import AudioDuplex
from nagents import Event
from nagents import ImageContent
from nagents.live import LiveAPI
from nagents.live import LiveConfig
from nagents.live import LiveEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents import Agent


class BrowserApp:
    def __init__(self, factory: Callable[[LiveConfig], Agent], config: LiveConfig, *, port: int = 3000) -> None:
        self.factory = factory
        self.config = config
        self.port = port
        self.token = secrets.token_urlsafe(32)
        self.agents: dict[str, Agent] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.events: deque[dict[str, object]] = deque(maxlen=256)
        self.app = web.Application(client_max_size=4 * 1024 * 1024)
        self.app.add_routes(
            [
                web.get("/", self.index),
                web.post("/session", self.create),
                web.post("/close", self.close),
                web.get("/state", self.state),
                web.post("/message", self.message),
                web.post("/command", self.command),
                web.post("/recording", self.recording),
            ]
        )
        self.app.on_cleanup.append(self.cleanup)

    def authorize(self, request: web.Request) -> None:
        if request.headers.get("X-Demo-Token") != self.token or request.host not in {
            f"localhost:{self.port}",
            f"127.0.0.1:{self.port}",
        }:
            raise web.HTTPForbidden()
        if request.method == "POST" and request.headers.get("Origin") not in {
            f"http://localhost:{self.port}",
            f"http://127.0.0.1:{self.port}",
        }:
            raise web.HTTPForbidden()

    async def index(self, request: web.Request) -> web.Response:
        html = Path(__file__).with_name("index.html").read_text().replace("__TOKEN__", self.token)
        return web.Response(text=html, content_type="text/html")

    async def create(self, request: web.Request) -> web.Response:
        self.authorize(request)
        if self.tasks:
            raise web.HTTPConflict(text="End the active conversation before starting another")
        data = await request.json()
        if not isinstance(data.get("sdp"), str) or not 0 < len(data["sdp"]) <= 65536:
            raise web.HTTPBadRequest(text="A bounded SDP offer is required")
        owner = self.factory(self.config)
        try:
            api = LiveAPI(owner.provider)
            source = str(data.get("fork_from", ""))
            if source:
                if source not in self.agents or not self.agents[source].live.status.finalized:
                    raise web.HTTPConflict(text="Fork requires an owned, finalized, stored source")
                result = await api.fork_webrtc(source, data["sdp"], store=bool(self.config.store))
            else:
                result = await api.create_webrtc(data["sdp"], owner.live_configuration(media=True))
            session = result["session"]
            if not isinstance(session, dict):
                raise ValueError("Malformed session creation response")
            identifier = str(session["id"])
            owner.provider.live_config = replace(owner.provider.live_config or self.config, attach_to=identifier)
            owner.audio = None  # Browser owns media; sideband only observes reflected audio.
            self.agents[identifier] = owner
            task = asyncio.create_task(self.observe(identifier, owner))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return web.json_response(result, status=201)
        except BaseException:
            await owner.close()
            if owner.delegation_agent:
                await owner.delegation_agent.close()
            raise

    async def observe(self, identifier: str, agent: Agent) -> None:
        try:
            async with aclosing(agent.run()) as events:
                async for event in events:
                    record = json.loads(json.dumps(asdict(event), default=str))
                    if isinstance(event, AudioChunkEvent):
                        record.pop("chunk", None)
                        record["base64_bytes"] = len(event.chunk)
                    elif isinstance(event, LiveEvent) and event.event_type == "session.input_audio.append":
                        record["payload"] = {"base64_bytes": len(str(event.payload.get("audio", "")))}
                    self.events.append({"session_id": identifier, "event": record})
                    await self.on_event(agent, event)
        except Exception as error:
            self.events.append({"session_id": identifier, "failure": type(error).__name__})
        finally:
            await agent.close()
            if agent.delegation_agent:
                await agent.delegation_agent.close()

    async def close(self, request: web.Request) -> web.Response:
        self.authorize(request)
        identifier = str((await request.json())["session_id"])
        owner = self.agents.get(identifier)
        if owner is None:
            raise web.HTTPNotFound()
        await owner.live.close()
        return web.json_response({"closing": True})

    async def state(self, request: web.Request) -> web.Response:
        self.authorize(request)
        return web.json_response({"events": list(self.events)})

    async def on_event(self, agent: Agent, event: Event) -> None:
        if isinstance(event, LiveEvent) and event.event_type == "connection.attached":
            await agent.add_instructions(
                "Greet the caller now in English, introduce yourself as an AI assistant, then listen."
            )

    async def message(self, request: web.Request) -> web.Response:
        self.authorize(request)
        data = await request.json()
        owner = self.agents.get(str(data.get("session_id", "")))
        if owner is None:
            raise web.HTTPNotFound()
        if data.get("image"):
            uri = str(data["image"])
            header, separator, content = uri.partition(",")
            if not separator or header not in {
                "data:image/png;base64",
                "data:image/jpeg;base64",
                "data:image/webp;base64",
            }:
                raise web.HTTPBadRequest(text="Use PNG, JPEG or WebP data URLs")
            identifier = await owner.live.submit_image(
                ImageContent(base64_data=content, media_type=header[5:].split(";")[0]),
                str(data.get("text") or "Describe this image for the caller."),
            )
        else:
            text = str(data.get("text", ""))
            if not text.strip():
                raise web.HTTPBadRequest(text="Text is required")
            identifier = await owner.live.submit_text(text)
        return web.json_response({"queued": identifier})

    async def command(self, request: web.Request) -> web.Response:
        self.authorize(request)
        data = await request.json()
        owner = self.agents.get(str(data.get("session_id", "")))
        if owner is None:
            raise web.HTTPNotFound()
        name = data.get("command")
        if name == "mute":
            identifier = await owner.live.mute_input()
        elif name == "unmute":
            identifier = await owner.live.unmute_input()
        elif name == "context":
            if data.get("invalidate"):
                owner.live.invalidate_tasks()
            identifier = await owner.add_thinking(str(data.get("text", "")))
        else:
            raise web.HTTPBadRequest(text="Unknown application command")
        await owner.live.wait(identifier)
        return web.json_response({"accepted": identifier})

    async def recording(self, request: web.Request) -> web.StreamResponse:
        self.authorize(request)
        identifier = str((await request.json())["session_id"])
        owner = self.agents.get(identifier)
        if owner is None or not owner.live.status.finalized:
            raise web.HTTPConflict(text="Recording requires a finalized stored session")
        response = web.StreamResponse(
            headers={"Content-Type": "audio/wav", "Content-Disposition": "attachment; filename=recording.wav"}
        )
        # Fetch before preparing headers so a service error is still an HTTP failure.
        async with aclosing(LiveAPI(owner.provider).recording(identifier)) as chunks:
            first = await anext(chunks)
            await response.prepare(request)
            await response.write(first)
            async for chunk in chunks:
                await response.write(chunk)
        await response.write_eof()
        return response

    async def cleanup(self, app: web.Application) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    cli = parser(__doc__ or "Browser Live")
    cli.add_argument("--port", type=int, default=3000)
    args = cli.parse_args()
    app = BrowserApp(lambda config: voice(config, audio=AudioDuplex()), LiveConfig(web_search=True), port=args.port)
    web.run_app(app.app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
