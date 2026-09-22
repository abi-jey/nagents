"""Verified inbound SIP calls, native accept/reject, sideband and controlled transfer."""

import asyncio
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _support import drive
from _support import parser
from _support import voice

from nagents import AudioDuplex
from nagents.live import LiveAPI
from nagents.live import LiveConfig
from nagents.live import verify_webhook


def main() -> None:
    cli = parser(__doc__ or "SIP")
    cli.add_argument("--port", type=int, default=8000)
    cli.add_argument("--reject", action="store_true")
    args = cli.parse_args()
    load_dotenv()
    secret = os.environ.get("OPENAI_WEBHOOK_SECRET", "")
    if not secret:
        cli.error("Set OPENAI_WEBHOOK_SECRET to verify incoming calls")
    database = sqlite3.connect("sip-deliveries.db")
    database.execute("CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, status TEXT)")
    database.commit()
    tasks: set[asyncio.Task[None]] = set()

    async def serve(identifier: str, delivery: str) -> None:
        config = LiveConfig(web_search=True)
        agent = voice(
            config,
            audio=AudioDuplex(),
            instructions="Be a concise phone assistant. Delegate reasoning and current-information requests. Transfer only when requested.",
        )
        api = LiveAPI(agent.provider)
        requested = False

        async def transfer_to_human() -> str:
            """Request transfer to the application's configured destination."""
            nonlocal requested
            if requested:
                return "Transfer was already requested; reconcile its outcome instead of retrying."
            target = os.environ.get("LIVE_TRANSFER_TARGET", "")
            if not target:
                return "Transfer is not configured."
            requested = True
            await api.refer(identifier, target)
            return "Transfer requested; this does not prove the destination answered."

        agent.register_tool(transfer_to_human)
        try:
            if args.reject:
                await api.reject(identifier)
            else:
                await api.accept(identifier, agent.live_configuration(media=True))
                agent.provider.live_config = replace(config, attach_to=identifier)
                await drive(agent)
            database.execute("UPDATE deliveries SET status='finished' WHERE id=?", (delivery,))
        except Exception as error:
            database.execute("UPDATE deliveries SET status='unknown' WHERE id=?", (delivery,))
            print("Reconcile call outcome:", identifier, type(error).__name__)
        finally:
            database.commit()
            await agent.close()

    async def incoming(request: web.Request) -> web.Response:
        try:
            event = verify_webhook(await request.read(), dict(request.headers), secret)
        except ValueError:
            raise web.HTTPUnauthorized() from None
        data = event.get("data", {})
        if event.get("type") not in {"live.transport.incoming", "live.call.incoming"}:
            return web.json_response({"ignored": True})
        if not isinstance(data, dict) or not data.get("session_id"):
            raise web.HTTPBadRequest(text="Missing Live session ID")
        if data.get("type", "sip") != "sip":
            return web.json_response({"ignored": True})
        delivery = request.headers["webhook-id"]
        claimed = database.execute("INSERT OR IGNORE INTO deliveries VALUES (?, 'claimed')", (delivery,))
        database.commit()
        if claimed.rowcount:
            task = asyncio.create_task(serve(str(data["session_id"]), delivery))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return web.json_response({"received": True})

    async def cleanup(app: web.Application) -> None:
        owned = list(tasks)
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)
        database.close()

    app = web.Application(client_max_size=65536)
    app.router.add_post("/webhooks/openai", incoming)
    app.on_cleanup.append(cleanup)
    web.run_app(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
