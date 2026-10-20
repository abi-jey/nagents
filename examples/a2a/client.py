"""Call the remote machine using an externally acquired API access token."""

from __future__ import annotations

import argparse
import asyncio

from a2a.helpers import new_text_message
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import TaskState

from nagents.a2a import RemoteAgent

from .tokens import Tokens


async def run(url: str, prompt: str, *, loopback_http: bool, authentication: str) -> None:
    message = new_text_message(prompt)
    message.role = Role.ROLE_USER
    # new_text_message creates a unique message ID. Retain it (and the exact
    # content) if your application needs to inspect an uncertain send outcome.
    print(f"Message: {message.message_id}")
    tokens = Tokens(authentication, loopback_http=loopback_http)
    async with RemoteAgent(url, tokens, loopback_http=loopback_http) as remote:
        async for event in remote.send(SendMessageRequest(message=message)):
            if event.HasField("task"):
                print(f"Task: {event.task.id} · {TaskState.Name(event.task.status.state)}")
            elif event.HasField("status_update"):
                status = event.status_update.status
                print(TaskState.Name(status.state))
                for part in status.message.parts:
                    if part.HasField("text"):
                        print(part.text)
            elif event.HasField("artifact_update"):
                for part in event.artifact_update.artifact.parts:
                    if part.HasField("text"):
                        print(part.text, end="", flush=True)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Fixed service origin, such as https://machine-agent.example.com")
    parser.add_argument("prompt", nargs="?", default="Inspect this machine's OS and filesystem capacity.")
    parser.add_argument("--loopback-http", action="store_true", help="Explicit local development only")
    parser.add_argument(
        "--authentication", choices=("environment", "client-credentials", "browser"), default="environment"
    )
    args = parser.parse_args()
    asyncio.run(run(args.url, args.prompt, loopback_http=args.loopback_http, authentication=args.authentication))


if __name__ == "__main__":
    main()
