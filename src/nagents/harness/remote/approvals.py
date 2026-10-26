"""Bridge native ngn action reviews through the local host's approval UI."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..private_store import ProtectedStoreError
from ..types import Notice

if TYPE_CHECKING:
    from nagents.a2a.client.transport import RemoteAgent

    from ..runtime.core import Harness

logger = logging.getLogger(__name__)


def _remote_operation_allowed(harness: Harness) -> bool:
    """Honor current root/child lifecycle and inherited to_agent restrictions."""
    root = harness.tasks.root.harness
    if (
        harness._closed
        or root._closed
        or harness.config.demo
        or root.config.demo
        or harness.mode != "build"
        or not harness.allow_subagents
        or not root.allow_subagents
    ):
        return False
    try:
        harness.refresh_tool_policy()
        return harness.tool_enabled("to_agent")
    except (OSError, ValueError, RuntimeError):
        return False


def _workspace_trust_allows(harness: Harness, remote: RemoteAgent) -> bool:
    """Trust only the currently accepted binding after local policy passed."""
    active = harness._remote_profile
    if not active.configured or remote.url != active.url:
        return False
    try:
        catalog = harness._agent_catalog.remote_store.load()
    except (OSError, ProtectedStoreError, ValueError):
        return False
    accepted = catalog.agents.get(harness.config.agent)
    return (
        accepted is not None
        and accepted.a2a.fingerprint == active.fingerprint
        and accepted.a2a.fingerprint in catalog.trusted_connections
    )


async def review_action(harness: Harness, remote: RemoteAgent, task_id: str, text: str) -> bool:
    """Handle ngn's advertised review link; other A2A servers retain their own UI.

    The bearer guard pins the origin. Task identity, nonce and digest are checked
    by the remote service; nonce/digest never enter model or browser evidence.
    A local saved tool allowance cannot silently approve a remote operation.
    """
    prefix = "Action approval required. Review the proposed operation at "
    if not text.startswith(prefix):
        return False
    url = text[len(prefix) :]
    target, origin = urlsplit(url), urlsplit(remote.url)
    if (
        (target.scheme, target.netloc) != (origin.scheme, origin.netloc)
        or target.query
        or target.fragment
        or not re.fullmatch(r"/authorize/[A-Za-z0-9_-]{16,128}", target.path)
    ):
        return False
    response = await remote.http.get(url, headers={"Accept": "application/json"})
    response.raise_for_status()
    if len(response.content) > 65536:
        raise ValueError("Remote review exceeds the supported size")
    record = response.json()
    if (
        not isinstance(record, dict)
        or record.get("task_id") != task_id
        or record.get("kind") != "action"
        or not isinstance(record.get("nonce"), str)
        or not isinstance(record.get("digest"), str)
        or not isinstance(record.get("operation"), dict)
    ):
        raise ValueError("Remote action review does not match the running task")
    operation = record["operation"]
    encoded = json.dumps(operation, ensure_ascii=True, allow_nan=False)
    if len(encoded) > 32768:
        raise ValueError("Remote operation exceeds the supported size")
    tool_name = operation.get("tool")
    safe_tool_name = (
        tool_name if isinstance(tool_name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool_name) else "unknown"
    )
    allowed = _remote_operation_allowed(harness)
    approved = allowed and _workspace_trust_allows(harness, remote)
    if approved:
        logger.info(
            "Remote operation approved by workspace trust: agent=%s tool=%s policy=workspace_remote_tool_trust",
            harness.config.agent,
            safe_tool_name,
        )
        await harness.emit(
            Notice(f"Workspace trust automatically approved remote tool {safe_tool_name} for {harness.config.agent}.")
        )
    elif allowed:
        try:
            await harness.approve(
                "to_agent",
                {"agent": harness.config.agent, "remote_operation": operation},
                f"Remote agent {harness.config.agent} requests an operation: {safe_tool_name}",
                preview=encoded,
                mandatory=True,
            )
            approved = True
        except PermissionError:
            pass
    else:
        logger.info(
            "Remote operation denied by local policy: agent=%s tool=%s policy=to_agent_disabled",
            harness.config.agent,
            safe_tool_name,
        )
    decision = await remote.http.post(
        url + "/decision",
        headers={"Accept": "application/json"},
        json={"approved": approved, "nonce": record["nonce"], "digest": record["digest"]},
    )
    decision.raise_for_status()
    return True
