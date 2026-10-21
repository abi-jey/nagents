"""Run an A2A conversation through the harness without invoking a local model."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import aclosing
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiosqlite

from nagents._async import finish_on_cancel
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


@asynccontextmanager
async def owned_remote_task(
    remote: RemoteAgent, conversation: RemoteConversation, harness: Harness, message_id: str
) -> AsyncIterator[None]:
    """A harness Stop cancels its owned remote task before closing transport."""
    try:
        yield
    except asyncio.CancelledError:
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
        yield ErrorEvent(
            message="Install nagents[a2a] on the ngn server to connect remote agents", code="A2A_UNAVAILABLE"
        )
        return
    profile = harness._remote_profile
    if profile.issuer and not profile.client_id:
        yield ErrorEvent(
            message="The remote host has not advertised a public sign-in client. Its administrator must configure that client and register ngn's callback URL in the identity provider.",
            code="A2A_HOST_SETUP",
        )
        return
    if harness.config.demo or harness.mode == "reviewer":
        yield ErrorEvent(
            message="Remote execution is unavailable under this runtime's read-only or demo ceiling",
            code="A2A_PERMISSION",
        )
        return
    credentials = RemoteCredentials(harness.agent.session.db_path.parent, profile)
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
                yield ErrorEvent(
                    message="This conversation belongs to a different remote connection. Start a new chat to use the changed connection.",
                    code="A2A_BINDING_CHANGED",
                )
                return
            if row[4]:
                yield ErrorEvent(
                    message="The previous remote request has an uncertain outcome. It was not sent again. Inspect the remote task before starting another request.",
                    code="A2A_OUTCOME_UNKNOWN",
                )
                return
            context_id = str(row[1])
            task_id = "" if row[3] in TERMINAL_STATES else str(row[2])
    text = ""
    state: int = TaskState.TASK_STATE_UNSPECIFIED
    identity: dict[str, object] = {"response_id": response_id, "response_session_id": session_id}
    try:
        # Credential/TLS validation happens before dispatching to the remote host.
        if profile.issuer or profile.token_env:
            await credentials.token()
        tls = credentials.tls()
        if harness._is_subagent:
            await harness.approve(
                "delegate",
                {"agent": harness.config.agent, "url": profile.url, "prompt": prompt},
                f"Send this task to remote agent {harness.config.agent} at {profile.url}",
            )
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
            last_status = ""
            async with (
                owned_remote_task(remote, conversation, harness, message_id),
                aclosing(conversation.exchange(prompt, message_id=message_id)) as updates,
            ):
                async for update in updates:
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
            if state not in TERMINAL_STATES and state != TaskState.TASK_STATE_INPUT_REQUIRED:
                raise RuntimeError("Remote stream ended before a terminal state")
            async with aiosqlite.connect(harness.agent.session.db_path) as db:
                await db.execute(
                    "UPDATE ngn_remote_sessions SET pending=0 WHERE session_id=? AND message_id=?",
                    (session_id, message_id),
                )
                await db.commit()
            if state not in {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_INPUT_REQUIRED}:
                yield ErrorEvent(
                    message="The remote agent did not complete this request. Check its task status.",
                    code="A2A_TASK_FAILED",
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
        logger.warning(
            "A2A execution failed agent=%s session=%s message=%s task=%s error_type=%s",
            harness.config.agent,
            session_id,
            message_id,
            task_id,
            type(error).__name__,
        )
        yield ErrorEvent(
            message="Remote agent connection failed or needs sign-in. Open Connection settings. The request was not automatically retried.",
            code="A2A_CONNECTION",
        )
