from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import suppress

from nagents.tools import ToolRegistry

from .protocol import MAX_FRAME
from .protocol import HostFrame
from .protocol import ProtocolError
from .protocol import fields
from .protocol import output
from .protocol import read
from .runtime import HostExecutor
from .runtime import Start
from .runtime import run_agent


async def serve() -> int:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_FRAME)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    task: asyncio.Task[None] | None = None
    incoming: asyncio.Task[HostFrame] | None = None
    try:
        output("", {"type": "ready", "runtime": "nagents", "capabilities": ["host_tools", "cancel", "safe_steering"]})
        first = await read(reader)
        start = Start.parse(first.message)
        executor = HostExecutor(ToolRegistry(), first.run_id, output)
        task = asyncio.create_task(run_agent(start, executor, output))
        while not task.done():
            incoming = asyncio.create_task(read(reader))
            done, _ = await asyncio.wait((task, incoming), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                incoming.cancel()
                with suppress(asyncio.CancelledError):
                    await incoming
                break
            frame = await incoming
            if frame.run_id != first.run_id:
                raise ProtocolError("Host frame has the wrong run_id")
            message = frame.message
            kind = message.get("type")
            if kind == "tool_result":
                executor.accept(message)
            elif kind == "steering_offer":
                executor.steering.accept(message)
            elif kind == "cancel":
                fields(message, {"type"})
                task.cancel()
                break
            else:
                raise ProtocolError("Unexpected host message")
        await task
        return 0
    finally:
        if incoming is not None and not incoming.done():
            incoming.cancel()
            with suppress(asyncio.CancelledError):
                await incoming
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        transport.close()


def main() -> int:
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
    try:
        return asyncio.run(serve())
    except (ProtocolError, EOFError, BrokenPipeError):
        print("ngn-nagents: invalid/closed host protocol stream", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
