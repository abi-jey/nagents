"""Run an A2A conversation through the harness without invoking a local model."""

from __future__ import annotations

import asyncio
import logging
import ssl
import uuid
from contextlib import aclosing
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiosqlite

from nagents._async import finish_on_cancel
from nagents.a2a.errors import RemoteAgentError
from nagents.error_presentation import remote_error_message
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import ResponseMetadataEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.types import Message

from .remote_credentials import RemoteCredentials
from .types import Notice

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator

    from nagents.a2a.client import RemoteAgent
    from nagents.a2a.conversation import RemoteConversation

    from .runtime import Harness
    from .types import HarnessEvent

logger = logging.getLogger(__name__)


def failure_event(
    harness: Harness,
    code: str,
    *,
    phase: str,
    outcome: str = "not_sent",
    message_id: str = "",
    task_received: bool = False,
    error_type: str = "",
) -> ErrorEvent:
    """Keep one safe reason in events, parent notifications and correlated logs."""
    event = ErrorEvent(message="", code=code, extra={"remote_phase": phase, "remote_outcome": outcome})
    event.message = remote_error_message(event)
    if not event.message:
        event.code = "A2A_RUNTIME"
        event.message = remote_error_message(event)
    logger.warning(
        "A2A request failed: agent=%s session=%s message=%s code=%s phase=%s outcome=%s task_received=%s error_type=%s",
        harness.config.agent,
        harness.session_id,
        message_id,
        event.code,
        phase,
        outcome,
        task_received,
        error_type,
    )
    return event


@asynccontextmanager
async def owned_remote_task(
    remote: RemoteAgent, conversation: RemoteConversation, harness: Harness, message_id: str
) -> AsyncIterator[None]:
    """A harness Stop cancels its owned remote task before closing transport."""
    try:
        yield
    except (asyncio.CancelledError, GeneratorExit):
        from nagents.a2a.conversation import TERMINAL_STATES

        if conversation.task_id and conversation.state not in TERMINAL_STATES:

            async def cancel() -> None:
                try:
                    async with asyncio.timeout(10):
                        task = await remote.cancel(conversation.task_id)
                        if task.status.state in TERMINAL_STATES:
                            async with aiosqlite.connect(harness.agent.session.db_path) as db:
                                await db.execute(
                                    "UPDATE ngn_remote_sessions SET state=?,pending=0 WHERE session_id=? AND message_id=?",
                                    (task.status.state, harness.session_id, message_id),
                                )
                                await db.commit()
                except Exception as error:
                    logger.warning(
                        "A2A cancellation unconfirmed session=%s message=%s error_type=%s",
                        harness.session_id,
                        message_id,
                        type(error).__name__,
                    )

            await finish_on_cancel(cancel())
        raise


async def run_remote(harness: Harness, prompt: str) -> AsyncGenerator[HarnessEvent, None]:
    """Admit a stable message once; retain identity on failure instead of replaying."""
    from .runtime import _HarnessSession

    session_id = harness.session_id
    message = Message(role="user", content=prompt)
    if isinstance(harness.agent.session, _HarnessSession):
        harness.agent.session.prepare_input(session_id, message)
    await harness.agent.session.add_message(session_id, message)
    from .task_activity import ChildActivity

    for plugin in harness.agent.plugins:
        if isinstance(plugin, ChildActivity):
            await plugin.remote_started()
    try:
        from a2a.types import TaskState

        from nagents.a2a.client import RemoteAgent
        from nagents.a2a.conversation import TERMINAL_STATES
        from nagents.a2a.conversation import RemoteConversation
    except ModuleNotFoundError:
        yield failure_event(harness, "A2A_UNAVAILABLE", phase="dependency")
        return
    profile = harness._remote_profile
    if profile.issuer and not profile.client_id:
        yield failure_event(harness, "A2A_HOST_SETUP", phase="configuration")
        return
    if harness.config.demo or harness.mode == "reviewer":
        yield failure_event(harness, "A2A_PERMISSION", phase="permission")
        return
    credentials = RemoteCredentials(profile)
    message_id = str(uuid.uuid4())
    response_id = f"a2a-{message_id}"
    context_id, task_id = "", ""
    async with aiosqlite.connect(harness.agent.session.db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS ngn_remote_sessions (session_id TEXT PRIMARY KEY, "
            "binding TEXT NOT NULL, context_id TEXT NOT NULL, task_id TEXT NOT NULL, "
            "message_id TEXT NOT NULL, state INTEGER NOT NULL, pending INTEGER NOT NULL)"
        )
        await db.execute(
            "CREATE TRIGGER IF NOT EXISTS ngn_remote_sessions_delete BEFORE DELETE ON v2_sessions "
            "BEGIN DELETE FROM ngn_remote_sessions WHERE session_id = OLD.id; END"
        )
        async with db.execute(
            "SELECT binding, context_id, task_id, state, pending FROM ngn_remote_sessions WHERE session_id=?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            if row[0] != profile.fingerprint:
                yield failure_event(harness, "A2A_BINDING_CHANGED", phase="state")
                return
            if row[4]:
                yield failure_event(harness, "A2A_OUTCOME_UNKNOWN", phase="state", outcome="unknown")
                return
            context_id = str(row[1])
            task_id = "" if row[3] in TERMINAL_STATES else str(row[2])
    text = ""
    state: int = TaskState.TASK_STATE_UNSPECIFIED
    identity: dict[str, object] = {"response_id": response_id, "response_session_id": session_id}
    phase = "credentials"
    dispatched = False
    accepted_update = False
    try:
        # Credential/TLS validation happens before dispatching to the remote host.
        if profile.issuer or profile.token_env:
            await credentials.token()
        phase = "tls"
        tls = credentials.tls()
        if harness._is_subagent:
            phase = "approval"
            await harness.approve(
                "delegate",
                {"agent": harness.config.agent, "url": profile.url, "prompt": prompt},
                f"Send this task to remote agent {harness.config.agent} at {profile.url}",
            )
        phase = "discovery"
        async with RemoteAgent(
            profile.url,
            credentials.token if profile.issuer or profile.token_env else None,
            rpc_url=profile.rpc_url,
            loopback_http=profile.loopback_http,
            timeout=profile.timeout,
            verify=tls,
        ) as remote:
            conversation = RemoteConversation(remote, task_id=task_id, context_id=context_id)
            async with aiosqlite.connect(harness.agent.session.db_path) as db:
                await db.execute(
                    "INSERT INTO ngn_remote_sessions VALUES(?,?,?,?,?,?,1) ON CONFLICT(session_id) "
                    "DO UPDATE SET message_id=excluded.message_id,pending=1",
                    (session_id, profile.fingerprint, context_id, task_id, message_id, state),
                )
                await db.commit()
            logger.info(
                "A2A send admitted agent=%s session=%s message=%s", harness.config.agent, session_id, message_id
            )
            phase = "send"
            dispatched = True
            last_status = ""
            async with (
                owned_remote_task(remote, conversation, harness, message_id),
                aclosing(conversation.exchange(prompt, message_id=message_id)) as updates,
            ):
                async for update in updates:
                    phase = "response"
                    accepted_update = True
                    state, context_id, task_id = update.state, update.context_id, update.task_id
                    async with aiosqlite.connect(harness.agent.session.db_path) as db:
                        await db.execute(
                            "UPDATE ngn_remote_sessions SET context_id=?,task_id=?,state=? WHERE session_id=? AND message_id=?",
                            (context_id, task_id, state, session_id, message_id),
                        )
                        await db.commit()
                    if update.status_text and update.status_text != last_status:
                        last_status = update.status_text
                        from .remote_approvals import review_action

                        if not await review_action(harness, remote, task_id, update.status_text):
                            yield Notice(update.status_text)
                    if update.text != text:
                        if update.text.startswith(text):
                            yield TextChunkEvent(chunk=update.text[len(text) :], extra=dict(identity))
                        text = update.text
                    logger.debug(
                        "A2A event agent=%s session=%s message=%s task=%s state=%s text_bytes=%s",
                        harness.config.agent,
                        session_id,
                        message_id,
                        task_id,
                        state,
                        len(text.encode()),
                    )
            if state not in TERMINAL_STATES and state not in {
                TaskState.TASK_STATE_INPUT_REQUIRED,
                TaskState.TASK_STATE_AUTH_REQUIRED,
            }:
                raise RemoteAgentError("Remote stream ended before a terminal state", code="A2A_OUTCOME_UNKNOWN")
            async with aiosqlite.connect(harness.agent.session.db_path) as db:
                await db.execute(
                    "UPDATE ngn_remote_sessions SET pending=0 WHERE session_id=? AND message_id=?",
                    (session_id, message_id),
                )
                await db.commit()
            if state not in {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_INPUT_REQUIRED}:
                state_codes: dict[int, str] = {
                    TaskState.TASK_STATE_AUTH_REQUIRED: "A2A_AUTH_REQUIRED",
                    TaskState.TASK_STATE_REJECTED: "A2A_TASK_REJECTED",
                    TaskState.TASK_STATE_CANCELED: "A2A_TASK_CANCELLED",
                }
                code = state_codes.get(state, "A2A_TASK_FAILED")
                yield failure_event(
                    harness,
                    code,
                    phase="response",
                    outcome="known",
                    message_id=message_id,
                    task_received=bool(task_id),
                )
                return
            if state == TaskState.TASK_STATE_INPUT_REQUIRED and not text:
                text = conversation.status_text
            history_id = await harness.agent.session.add_message(session_id, Message(role="assistant", content=text))
            yield TextDoneEvent(text=text, extra=dict(identity))
            yield ResponseMetadataEvent(
                response_id=response_id, response_session_id=session_id, history_id=str(history_id), status="completed"
            )
            for plugin in harness.agent.plugins:
                if isinstance(plugin, ChildActivity):
                    await plugin.remote_finished()
            yield DoneEvent(final_text=text, session_id=session_id)
    except asyncio.CancelledError:
        # Never turn an interrupted local stream into an implicit repeated send.
        # The retained remote ID makes explicit inspection/cancellation possible.
        logger.info("A2A local stream cancelled session=%s message=%s task=%s", session_id, message_id, task_id)
        raise
    except Exception as error:
        code = (
            error.code
            if isinstance(error, RemoteAgentError)
            else "A2A_TLS"
            if phase == "tls" or isinstance(error, ssl.SSLError)
            else "A2A_PERMISSION"
            if phase == "approval" and isinstance(error, PermissionError)
            else "A2A_RUNTIME"
        )
        outcome = "unknown" if dispatched else "not_sent"
        if (
            dispatched
            and not accepted_update
            and code
            in {
                "A2A_AUTH_REQUIRED",
                "A2A_AUTH_REJECTED",
                "A2A_AUTH_STORAGE",
                "A2A_TOKEN_MISSING",
                "A2A_FORBIDDEN",
                "A2A_RATE_LIMIT",
            }
        ):
            try:
                async with aiosqlite.connect(harness.agent.session.db_path) as db:
                    cursor = await db.execute(
                        "UPDATE ngn_remote_sessions SET pending=0 WHERE session_id=? AND message_id=?",
                        (session_id, message_id),
                    )
                    await db.commit()
                    if cursor.rowcount == 1:
                        outcome = (
                            "rejected"
                            if code in {"A2A_AUTH_REJECTED", "A2A_FORBIDDEN", "A2A_RATE_LIMIT"}
                            else "not_sent"
                        )
            except Exception as record_error:
                logger.warning(
                    "A2A rejection state was not saved: session=%s message=%s error_type=%s",
                    session_id,
                    message_id,
                    type(record_error).__name__,
                )
        yield failure_event(
            harness,
            code,
            phase=phase,
            outcome=outcome,
            message_id=message_id,
            task_received=bool(task_id),
            error_type=type(error).__name__,
        )
