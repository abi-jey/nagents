"""Run an A2A conversation through the harness without invoking a local model."""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
import uuid
from contextlib import aclosing
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from typing import Literal

import aiosqlite

from nagents._async import finish_on_cancel
from nagents._async import join_owned
from nagents._sqlite import write_database
from nagents.a2a.errors import RemoteAgentError
from nagents.error_presentation import remote_error_message
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import ResponseMetadataEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.types import ContentPart
from nagents.types import Message
from nagents.types import TextContent

from ..types import Notice
from ..types import RemoteArtifacts
from .artifacts import image_references
from .credentials import RemoteCredentials

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator

    from nagents.a2a.client.conversation import RemoteConversation
    from nagents.a2a.client.transport import RemoteAgent
    from nagents.artifacts import ImageArtifact

    from ..runtime.core import Harness
    from ..types import HarnessEvent

logger = logging.getLogger(__name__)


async def _write_state(harness: Harness, statement: str, parameters: tuple[str | int, ...]) -> int:
    """Close an admitted remote-state transaction before emitting any event."""

    async def write() -> int:
        async with aiosqlite.connect(harness.agent.session.db_path) as db:
            async with db.execute(statement, parameters) as cursor:
                affected = cursor.rowcount
            await db.commit()
            return affected

    return await write_database(harness.agent.session.db_path, write)


async def _load_state(harness: Harness) -> tuple[str, str, str, int, int] | None:
    async def load() -> tuple[str, str, str, int, int] | None:
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
                (harness.session_id,),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
            return (str(row[0]), str(row[1]), str(row[2]), int(row[3]), int(row[4])) if row else None

    return await write_database(harness.agent.session.db_path, load)


async def save_output(
    harness: Harness,
    response_id: str,
    text: str,
    images: tuple[ImageArtifact, ...],
    status: Literal["completed", "cancelled", "failed"],
) -> list[HarnessEvent]:
    """Commit output once before publishing any authenticated image reference."""
    content: str | list[ContentPart] = text
    if images:
        content = [TextContent(text)] if text else []
        content.extend(image.content() for image in images)
    history_id = await harness.agent.session.add_message(harness.session_id, Message(role="assistant", content=content))
    identity = {"response_id": response_id, "response_session_id": harness.session_id}
    events: list[HarnessEvent] = [TextDoneEvent(text=text, extra=identity)]
    references = image_references(content, harness.tasks.root.harness.session_id, history_id)
    if references:
        events.append(RemoteArtifacts(response_id, harness.session_id, str(history_id), references))
    events.append(
        ResponseMetadataEvent(
            response_id=response_id, response_session_id=harness.session_id, history_id=str(history_id), status=status
        )
    )
    return events


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
        from nagents.a2a.client.conversation import TERMINAL_STATES

        if conversation.task_id and conversation.state not in TERMINAL_STATES:
            task_identity = (
                conversation.task_id if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", conversation.task_id) else "unavailable"
            )

            async def cancel() -> None:
                try:
                    logger.info(
                        "A2A cancellation requested session=%s message=%s task=%s",
                        harness.session_id,
                        message_id,
                        task_identity,
                    )
                    async with asyncio.timeout(10):
                        task = await remote.cancel(conversation.task_id)
                        if task.status.state in TERMINAL_STATES:
                            await _write_state(
                                harness,
                                "UPDATE ngn_remote_sessions SET state=?,pending=0 WHERE session_id=? AND message_id=?",
                                (task.status.state, harness.session_id, message_id),
                            )
                            logger.info(
                                "A2A cancellation confirmed session=%s message=%s task=%s state=%d",
                                harness.session_id,
                                message_id,
                                task_identity,
                                task.status.state,
                            )
                        else:
                            logger.warning(
                                "A2A cancellation unconfirmed session=%s message=%s task=%s state=%d",
                                harness.session_id,
                                message_id,
                                task_identity,
                                task.status.state,
                            )
                except Exception as error:
                    logger.warning(
                        "A2A cancellation unconfirmed session=%s message=%s task=%s error_type=%s",
                        harness.session_id,
                        message_id,
                        task_identity,
                        type(error).__name__,
                    )

            await finish_on_cancel(cancel())
        elif not conversation.task_id:
            logger.warning(
                "A2A cancellation unconfirmed session=%s message=%s task_received=0 outcome=unknown",
                harness.session_id,
                message_id,
            )
        raise


async def run_remote(harness: Harness, prompt: str) -> AsyncGenerator[HarnessEvent, None]:
    """Admit a stable message once; retain identity on failure instead of replaying."""
    from ..runtime.core import _HarnessSession

    session_id = harness.session_id
    message = Message(role="user", content=prompt)
    if isinstance(harness.agent.session, _HarnessSession):
        harness.agent.session.prepare_input(session_id, message)
    await harness.agent.session.add_message(session_id, message)
    from ..runtime.task_activity import ChildActivity

    for plugin in harness.agent.plugins:
        if isinstance(plugin, ChildActivity):
            await plugin.remote_started()
    try:
        from a2a.types import TaskState

        from nagents.a2a.client.conversation import TERMINAL_STATES
        from nagents.a2a.client.conversation import RemoteConversation
        from nagents.a2a.client.transport import RemoteAgent
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
    row = await _load_state(harness)
    # Event consumers can themselves write to this database. Never suspend the
    # async generator with an admitted transaction/connection still open.
    if row:
        if row[0] != profile.fingerprint:
            yield failure_event(harness, "A2A_BINDING_CHANGED", phase="state")
            return
        if row[4]:
            yield failure_event(harness, "A2A_OUTCOME_UNKNOWN", phase="state", outcome="unknown")
            return
        context_id = row[1]
        task_id = "" if row[3] in TERMINAL_STATES else row[2]
    text = ""
    state: int = TaskState.TASK_STATE_UNSPECIFIED
    identity: dict[str, object] = {"response_id": response_id, "response_session_id": session_id}
    phase = "credentials"
    dispatched = False
    accepted_update = False
    images: tuple[ImageArtifact, ...] = ()
    output_persistence: asyncio.Task[list[HarnessEvent]] | None = None
    conversation: RemoteConversation | None = None

    async def persist_output(status: Literal["completed", "cancelled", "failed"]) -> list[HarnessEvent]:
        nonlocal output_persistence
        if output_persistence is None:
            output_persistence = asyncio.create_task(save_output(harness, response_id, text, images, status))
        # Stop may arrive after SQLite commits but before its await resumes.
        # Always join this one owned operation; never infer that an interrupted
        # await means nothing was inserted and retry the assistant message.
        return await join_owned(output_persistence)

    try:
        # Credential/TLS validation happens before dispatching to the remote host.
        if profile.issuer or profile.token_env:
            await credentials.token()
        phase = "tls"
        tls = credentials.tls()
        if harness._is_subagent:
            phase = "approval"
            await harness.approve(
                "to_agent",
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
            try:
                await asyncio.to_thread(
                    harness._agent_catalog.remote_store.update_metadata,
                    profile.fingerprint,
                    remote.card.name[:200],
                    remote.card.description[:2000],
                )
            except Exception as error:
                logger.warning("A2A metadata update skipped error_type=%s", type(error).__name__)
            conversation = RemoteConversation(remote, task_id=task_id, context_id=context_id)
            await _write_state(
                harness,
                "INSERT INTO ngn_remote_sessions VALUES(?,?,?,?,?,?,1) ON CONFLICT(session_id) "
                "DO UPDATE SET message_id=excluded.message_id,pending=1",
                (session_id, profile.fingerprint, context_id, task_id, message_id, state),
            )
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
                    images = update.images
                    await _write_state(
                        harness,
                        "UPDATE ngn_remote_sessions SET context_id=?,task_id=?,state=? WHERE session_id=? AND message_id=?",
                        (context_id, task_id, state, session_id, message_id),
                    )
                    if update.status_text and update.status_text != last_status:
                        last_status = update.status_text
                        from .approvals import review_action

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
            await _write_state(
                harness,
                "UPDATE ngn_remote_sessions SET pending=0 WHERE session_id=? AND message_id=?",
                (session_id, message_id),
            )
            if state not in {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_INPUT_REQUIRED}:
                state_codes: dict[int, str] = {
                    TaskState.TASK_STATE_AUTH_REQUIRED: "A2A_AUTH_REQUIRED",
                    TaskState.TASK_STATE_REJECTED: "A2A_TASK_REJECTED",
                    TaskState.TASK_STATE_CANCELED: "A2A_TASK_CANCELLED",
                }
                code = state_codes.get(state, "A2A_TASK_FAILED")
                if images:
                    saved = await persist_output(
                        "cancelled" if state == TaskState.TASK_STATE_CANCELED else "failed",
                    )
                    for event in saved:
                        yield event
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
            saved = await persist_output("completed")
            for event in saved:
                yield event
            for plugin in harness.agent.plugins:
                if isinstance(plugin, ChildActivity):
                    await plugin.remote_finished()
            yield DoneEvent(final_text=text, session_id=session_id)
    except (asyncio.CancelledError, GeneratorExit):
        # Never turn an interrupted local stream into an implicit repeated send.
        # The retained remote ID makes explicit inspection/cancellation possible.
        if images:

            async def retain() -> None:
                saved = await persist_output("cancelled")
                for event in saved:
                    if isinstance(event, RemoteArtifacts):
                        for plugin in harness.agent.plugins:
                            if isinstance(plugin, ChildActivity):
                                await plugin.remote_cancelled_output(event)

            try:
                await finish_on_cancel(retain())
            except Exception as save_error:
                logger.warning(
                    "A2A stopped image output was not saved: session=%s message=%s error_type=%s",
                    session_id,
                    message_id,
                    type(save_error).__name__,
                )
        logger.info("A2A local stream stopped session=%s message=%s task=%s", session_id, message_id, task_id)
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
        if images and output_persistence is None:
            try:
                saved = await persist_output("failed")
                for event in saved:
                    yield event
            except Exception as save_error:
                logger.warning(
                    "A2A partial image output was not saved: session=%s message=%s error_type=%s",
                    session_id,
                    message_id,
                    type(save_error).__name__,
                )
        if code.startswith("A2A_ARTIFACT_") and conversation is not None and conversation.state in TERMINAL_STATES:
            try:
                await _write_state(
                    harness,
                    "UPDATE ngn_remote_sessions SET pending=0 WHERE session_id=? AND message_id=?",
                    (session_id, message_id),
                )
                outcome = "known"
            except Exception as save_error:
                logger.warning(
                    "A2A artifact outcome was not saved: session=%s error_type=%s",
                    session_id,
                    type(save_error).__name__,
                )
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
                affected = await _write_state(
                    harness,
                    "UPDATE ngn_remote_sessions SET pending=0 WHERE session_id=? AND message_id=?",
                    (session_id, message_id),
                )
                if affected == 1:
                    outcome = (
                        "rejected" if code in {"A2A_AUTH_REJECTED", "A2A_FORBIDDEN", "A2A_RATE_LIMIT"} else "not_sent"
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
