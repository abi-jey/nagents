"""Bridge native ngn action reviews through the local host's approval UI."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from nagents.a2a.client import RemoteAgent

    from .runtime import Harness


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
    approved = False
    try:
        await harness.approve(
            "delegate",
            {"agent": harness.config.agent, "remote_operation": operation},
            f"Remote agent {harness.config.agent} requests an operation: {operation.get('tool', 'action')}",
            preview=encoded,
            mandatory=True,
        )
        approved = True
    except PermissionError:
        pass
    decision = await remote.http.post(
        url + "/decision",
        headers={"Accept": "application/json"},
        json={"approved": approved, "nonce": record["nonce"], "digest": record["digest"]},
    )
    decision.raise_for_status()
    return True
