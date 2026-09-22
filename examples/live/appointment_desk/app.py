"""Real local appointment records, explicit UI approval, native client delegation and browser audio."""

import asyncio
import json
import sys
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.live._support import parser
from examples.live._support import voice
from examples.live.appointment_desk.store import Desk
from examples.live.webrtc.server import BrowserApp
from nagents import Agent
from nagents import AudioDuplex
from nagents import CodexProvider
from nagents import DoneEvent
from nagents import Event
from nagents import SessionManager
from nagents.live import LiveConfig
from nagents.live import LiveEvent

if TYPE_CHECKING:
    from collections.abc import Callable


class AppointmentApp(BrowserApp):
    def __init__(self, path: Path, port: int, *, mode: str = "client", store: bool = False) -> None:
        self.desk = Desk(path)
        self.background: set[asyncio.Task[None]] = set()
        super().__init__(
            self.agent, LiveConfig(delegation="client" if mode == "client" else "responses", store=store), port=port
        )
        self.app.router.add_post("/approve", self.approve)
        self.app.router.add_post("/job", self.job)
        self.app.router.add_post("/selection", self.selection)
        self.app.on_cleanup.append(self.close_jobs)

    def agent(self, config: LiveConfig) -> Agent:
        state = self.desk.state()
        context = json.dumps({"draft": state["draft"], "bookings": state["bookings"]})
        config = replace(
            config,
            history=(
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Authoritative saved application state: " + context}],
                },
            ),
        )
        owner = voice(
            config,
            audio=AudioDuplex(),
            instructions=(
                "You are an appointment desk assistant. Be concise. Delegate appointment work. "
                "Explain that the caller must approve the exact proposal in the UI. Announce completion only from verified backend results."
            ),
        )
        tools: list[Callable[..., object]] = [
            self.desk.availability,
            self.desk.propose_booking,
            self.desk.propose_change,
            self.desk.propose_cancellation,
            self.desk.commit,
            self.desk.get_report,
            owner.add_comment,
        ]
        if config.delegation == "responses":
            for tool in tools:
                owner.register_tool(tool)
            return owner
        backend = Agent(
            provider=CodexProvider(),
            session_manager=SessionManager(Path("desk-history.db")),
            streaming=True,
            tools=tools,
            system_prompt=(
                "Handle the current appointment request and corrections. Query authoritative state. "
                "Proposal is not completion; application approval is required. Never claim a booking before commit succeeds. "
                "Use optional commentary for progress. Return concise verified status."
            ),
        )
        owner.delegation_agent = backend
        return owner

    async def on_event(self, agent: Agent, event: Event) -> None:
        if isinstance(event, LiveEvent) and event.event_type == "connection.attached":
            text = "You are speaking with an AI appointment assistant. No appointment changes will be made without your approval."
            command = await agent.add_instructions(
                "Immediately say this disclosure exactly and fully, then ask how you can help: " + text
            )
            self.events.append({"disclosure_requested": command, "delivery_verified": False})
        elif isinstance(event, LiveEvent) and event.event_type == "session.instructions.appended":
            self.events.append(
                {"instruction_accepted": event.payload.get("client_event_id"), "delivery_verified": False}
            )
        elif isinstance(event, LiveEvent) and event.event_type == "session.closed":
            self.desk.record_session(agent.live.status.session_id, asdict(agent.live.status))

    async def selection(self, request: web.Request) -> web.Response:
        self.authorize(request)
        slot = str((await request.json())["slot"])
        result = self.desk.propose_booking(slot)
        for agent in self.agents.values():
            try:
                agent.live.invalidate_tasks()
                await agent.add_thinking(
                    "The user changed their selection. Old confirmations are invalid. Current proposal: " + result
                )
            except RuntimeError:
                pass
        return web.json_response({"proposal": json.loads(result)})

    async def job(self, request: web.Request) -> web.Response:
        self.authorize(request)
        identifier = self.desk.create_job()
        snapshot = self.desk.availability()

        async def work() -> None:
            try:
                agent = Agent(provider=CodexProvider(), session_manager=SessionManager(Path("desk-reports.db")))
            except Exception:
                self.desk.finish_job(identifier, "failed", "Backend could not be initialized.")
                return
            try:
                result = ""
                async for event in agent.run(
                    "Produce a concise review of these appointment records. Do not change anything: " + snapshot
                ):
                    self.events.append(
                        {"job_id": identifier, "event": json.loads(json.dumps(asdict(event), default=str))}
                    )
                    if isinstance(event, DoneEvent):
                        result = event.final_text
                self.desk.finish_job(identifier, "completed" if result else "failed", result)
            except asyncio.CancelledError:
                self.desk.finish_job(identifier, "interrupted", "Server stopped; this report was not completed.")
                raise
            except Exception:
                self.desk.finish_job(identifier, "failed", "Report generation failed.")
            finally:
                await agent.close()

        task = asyncio.create_task(work())
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return web.json_response({"job_id": identifier, "status": "running independently of voice"})

    async def close_jobs(self, app: web.Application) -> None:
        tasks = list(self.background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def state(self, request: web.Request) -> web.Response:
        self.authorize(request)
        return web.json_response({"events": list(self.events), "desk": self.desk.state()})

    async def approve(self, request: web.Request) -> web.Response:
        self.authorize(request)
        try:
            self.desk.approve(int((await request.json())["revision"]))
        except (ValueError, KeyError) as error:
            raise web.HTTPConflict(text=str(error)) from None
        for owner in self.agents.values():
            try:
                if owner.live.sender is not None:
                    await owner.add_thinking(
                        "The application has approved the current appointment proposal. Check its current revision before committing."
                    )
            except RuntimeError:
                pass  # Approval is durable even if the voice notification cannot be sent.
        return web.json_response({"approved": True})


def main() -> None:
    cli = parser(__doc__ or "Appointment desk")
    cli.add_argument("--database", type=Path, default=Path("appointments.db"))
    cli.add_argument("--port", type=int, default=3000)
    cli.add_argument("--mode", choices=("client", "responses"), default="client")
    cli.add_argument("--store", action="store_true", help="Enable remote recording/forking when the project permits it")
    args = cli.parse_args()
    server = AppointmentApp(args.database, args.port, mode=args.mode, store=args.store)
    web.run_app(server.app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
