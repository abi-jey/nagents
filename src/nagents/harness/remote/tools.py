"""Explicit workspace agent configuration through the normal host approval gate."""

from __future__ import annotations

import asyncio
import importlib.util
from typing import TYPE_CHECKING

from ..config import AgentProfile
from .profiles import RemoteProfile

if TYPE_CHECKING:
    from nagents.types import JsonValue

    from ..runtime import Harness


class RemoteAgentTools:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness

    async def remote_agent_configure(
        self,
        name: str,
        url: str,
        description: str = "",
        rpc_url: str = "",
        issuer: str = "",
        client_id: str = "",
        token_env: str = "",
        ca_file: str = "",
    ) -> dict[str, JsonValue]:
        """Add or update a workspace A2A agent. It reloads before the next model round.

        The remote server owns its prompt, model and tools. Never put tokens in
        arguments or files: token_env names a server variable; issuer/client_id
        enables browser sign-in in the agent's Connection settings. HTTPS is
        required. ca_file optionally names a server-side trusted CA bundle.
        With only url (and optional ca_file), discover the host's issuer,
        public client ID and scopes automatically. These are host/provider
        settings, not local model settings. After saving, use to_agent(agent=name).
        No restart is needed. Sign-in happens through the UI; never request tokens.
        """
        if self.harness._is_subagent or self.harness.mode != "build" or self.harness.config.demo:
            raise PermissionError("Only the root build agent can configure workspace remote connections")
        if name == "assistant":
            raise ValueError("Create a named remote agent instead of replacing the workspace assistant")
        if importlib.util.find_spec("a2a") is None:
            raise ValueError("Install nagents[a2a] on the ngn server to connect remote agents")
        profile = RemoteProfile(url, rpc_url, token_env, ca_file, issuer, client_id)
        AgentProfile(description=description, a2a=profile).validate()
        store = self.harness._agent_catalog.remote_store
        current = store.load()
        if name in self.harness.config.profiles and not self.harness.config.profiles[name].a2a.configured:
            raise ValueError("This name belongs to a local agent; choose a new name")
        await self.harness.approve(
            "remote_agent_configure",
            {
                "name": name,
                "url": url,
                "rpc_url": rpc_url,
                "issuer": issuer,
                "client_id": client_id,
                "token_env": token_env,
                "ca_file": ca_file,
                "description": description,
            },
            f"Save workspace remote agent {name} connected to {url}",
            call_id=self.harness.tools.call_id.get(),
        )
        discovered_name = discovered_description = ""
        if not issuer and not token_env:
            from nagents.a2a.discovery import discover_remote

            discovery = await discover_remote(profile)
            profile = discovery.connection
            discovered_name, discovered_description = discovery.name, discovery.description
            if discovery.authentication == "manual":
                raise ValueError(
                    "The remote host requires authentication that is not advertised as browser sign-in. Ask its administrator to publish its OAuth/OpenID Connect metadata."
                )
        store.save(
            {**current.agents, name: AgentProfile(description=description, a2a=profile)}, expected=current.revision
        )
        if not issuer and not token_env:
            await asyncio.to_thread(store.update_metadata, profile.fingerprint, discovered_name, discovered_description)
        self.harness.refresh_agent_definitions()
        return {
            "agent": name,
            "source": str(store.path),
            "transport": "a2a",
            "applies": "next_model_round",
            "sign_in_required": bool(profile.issuer),
            "host_setup_required": bool(profile.issuer and not profile.client_id),
            "sign_in_path": f"Settings → workspace → {name} → Remote connection → Sign in",
            "message": "Agent saved in workspace a2a.json. It is available on the next round without a restart. Do not send a task merely to check discovery; inspect the to_agent target schema. If sign-in is required, use the sign_in_path in the UI.",
        }
