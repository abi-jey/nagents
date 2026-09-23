"""Foundry GPT-Live: --mode hosted|client, --auth entra|key (see authentication docs)."""

import argparse
import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path

from _support import drive
from _support import microphone

from nagents import Agent
from nagents import FoundryProvider
from nagents import LiveConfig
from nagents import SessionManager


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("hosted", "client"), default="hosted")
    parser.add_argument("--auth", choices=("entra", "key"), default="entra")
    parser.add_argument(
        "--identity", choices=("default", "managed", "workload", "service-principal"), default="default"
    )
    parser.add_argument("--endpoint", default=os.environ.get("FOUNDRY_ENDPOINT", ""))
    parser.add_argument("--deployment", default=os.environ.get("FOUNDRY_LIVE_DEPLOYMENT", "gpt-live-1"))
    parser.add_argument("--backend", default=os.environ.get("FOUNDRY_BACKEND_DEPLOYMENT", "gpt-5.5"))
    parser.add_argument("--scope", default=os.environ.get("FOUNDRY_TOKEN_SCOPE", "https://ai.azure.com/.default"))
    parser.add_argument("--duration", type=float, default=60)
    args = parser.parse_args()
    if not args.endpoint:
        parser.error("Set --endpoint to https://RESOURCE.openai.azure.com/openai/v1")

    async with AsyncExitStack() as stack:
        credential = None
        api_key = ""
        if args.auth == "entra":
            # Application-selected SDK credential; Nagents implements no identity chain.
            from azure.identity.aio import ClientSecretCredential
            from azure.identity.aio import DefaultAzureCredential
            from azure.identity.aio import ManagedIdentityCredential
            from azure.identity.aio import WorkloadIdentityCredential

            selected: (
                DefaultAzureCredential | ManagedIdentityCredential | WorkloadIdentityCredential | ClientSecretCredential
            )
            if args.identity == "managed":
                selected = ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID"))
            elif args.identity == "workload":
                selected = WorkloadIdentityCredential()
            elif args.identity == "service-principal":
                selected = ClientSecretCredential(
                    os.environ["AZURE_TENANT_ID"], os.environ["AZURE_CLIENT_ID"], os.environ["AZURE_CLIENT_SECRET"]
                )
            else:
                selected = DefaultAzureCredential()
            await stack.enter_async_context(selected)
            credential = selected
        else:
            api_key = os.environ["FOUNDRY_API_KEY"]

        config = LiveConfig(delegation="responses" if args.mode == "hosted" else "client", backend_model=args.backend)
        sessions = SessionManager(Path("foundry-live.db"))
        backend = None
        if args.mode == "client":
            backend = Agent(
                provider=FoundryProvider(
                    model=args.backend,
                    base_url=args.endpoint,
                    api_key=api_key,
                    credential=credential,
                    scope=args.scope,
                ),
                session_manager=sessions,
                system_prompt="Answer the latest voice request briefly and accurately.",
            )
            stack.push_async_callback(backend.close)
        voice = Agent(
            provider=FoundryProvider(
                model=args.deployment,
                base_url=args.endpoint,
                live_config=config,
                api_key=api_key,
                credential=credential,
                scope=args.scope,
            ),
            session_manager=sessions,
            delegation_agent=backend,
            system_prompt="Be concise. Delegate requests requiring reasoning.",
            audio=microphone(),
        )
        stack.push_async_callback(voice.close)
        await drive(voice, duration=args.duration)


if __name__ == "__main__":
    asyncio.run(main())
