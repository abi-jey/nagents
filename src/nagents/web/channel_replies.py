"""Opt-in channel messages from genuine runs in permanently chat-owned sessions.

This is an approval decision only. HarnessExecutor still owns argument/schema,
profile and post-approval callable checks; ChannelHost owns outbound scoping.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from copy import deepcopy
from types import MethodType
from typing import TYPE_CHECKING

from .channel_host import ChannelHost

if TYPE_CHECKING:
    import sqlite3

    from nagents.harness.types import ApprovalRequest

    from .service import Run
    from .service import WebState

# Capture the implementation identities at import, rather than accepting a
# same-named replacement or looking up a potentially replaced instance method.
_ORIGINAL = {"channel_send": ChannelHost.channel_send, "channel_list": ChannelHost.channel_list}


async def automatic_reply(state: WebState, run: Run, request: ApprovalRequest) -> bool:
    host = state.channels
    harness = state.running_harness
    task = asyncio.current_task()
    producer = run.task
    ingress = state.history.ingress.get()
    original_work = ingress.work if ingress is not None else None
    if request.tool not in _ORIGINAL or request.task_id or request.depth or not request.id:
        return False

    def executing() -> bool:
        # The root Harness installs _worker for its own Agent.run producer and
        # clears it on join. A live WebState.active/background flag alone is not
        # execution authority; inherited context in another task is not either.
        if not (
            task is not None
            and harness._worker is task
            and not task.cancelling()
            and harness._busy == "run"
            and harness._queue is not None
            and harness.agent._active_runs > 0
            and harness.tasks._active
            and harness.tasks._session_id == run.session_id == harness.session_id
            and harness.tools.call_id.get() == request.id
            and state.active is run
            and run.task is producer
            and not run.finished
            and not producer.done()
            and not producer.cancelling()
            and run.pending is None
            and not run.chain.cancelled
            and state.history.ingress.get() is ingress
            and (ingress is None or ingress.work is original_work)
        ):
            return False
        if ingress is not None:
            work = ingress.work
            expected = (
                {"channel": work.channel, "conversation_id": work.conversation_id, "thread_id": work.thread_id}
                if work.channel
                else {}
            )
            return (
                ingress.owner is task
                and ingress.consumed
                and ingress.run_id == run.id
                and run.session_id == work.session_id
                and run.message_id == work.message_id
                and run.server_owned is True
                and run.background is False
                and not work.acknowledgement
                and run.source == expected
            )
        # Queued runs cannot discard their ingress and masquerade as a wakeup.
        # Real source-less runs are direct web execution or a scheduler-claimed
        # activation (the scheduler increments the chain before running it).
        return (
            run.server_owned is False
            and not run.source
            and not run.message_id
            and (run.background is False or (run.background is True and run.chain.activations > 0))
        )

    if not executing():
        return False
    definition = harness.agent.tool_registry.get(request.tool)
    if definition is None or not any(tool is definition for tool in host.tools):
        return False
    function = definition.func
    if (
        type(function) is not MethodType
        or function.__self__ is not host
        or function.__func__ is not _ORIGINAL[request.tool]
    ):
        return False
    arguments = deepcopy(request.arguments)
    try:
        owner = await host.store.owner(run.session_id)
    except Exception:
        return False
    if owner is None or owner.conflicted or not owner.channel or not owner.conversation_id or not executing():
        return False
    channel = owner.channel
    # Real ingress retains its stronger envelope, source and durable journal
    # checks. Web followups have a web inbox row; wakeups invent no ingress.
    if (
        ingress is not None
        and ingress.work.channel
        and (ingress.work.channel != channel or ingress.work.conversation_id != owner.conversation_id)
    ):
        return False
    connection = host.catalog.connections.get(channel)
    source = host.sources.get(channel)
    transport = host.channels.get(channel)
    runtime = host.runtime

    def eligible() -> bool:
        return (
            executing()
            and not host.closed
            and host.catalog.allow_plugins
            and not harness.config.demo
            and connection is not None
            and connection.enabled is True
            and connection.auto_reply is True
            and host.catalog.connections.get(channel) is connection
            and host.catalog.status.get(channel) == ("running", "")
            and source is not None
            and not source.done()
            and not source.cancelling()
            and host.sources.get(channel) is source
            and transport is not None
            and host.channels.get(channel) is transport
            and runtime is not None
            and host.runtime is runtime
            and runtime._active
            and any(binding.name == channel and binding.channel is transport for binding in runtime._bindings)
            and harness.agent.tool_registry.get(request.tool) is definition
            and definition.func is function
            and definition.name == request.tool
            and request.arguments == arguments
        )

    if not eligible():
        return False
    if request.tool == "channel_list":
        if arguments:
            return False
    elif (
        arguments.get("channel") != channel
        or arguments.get("destination") != owner.conversation_id
        or not isinstance(arguments.get("thread_id", ""), str)
        or not isinstance(arguments.get("reply_to", ""), str)
        or not isinstance(arguments.get("text"), str)
        or arguments.keys() - {"channel", "destination", "text", "thread_id", "reply_to"}
    ):
        return False
    if (
        request.tool == "channel_send"
        and ingress is not None
        and ingress.work.channel
        and (
            arguments.get("thread_id", "") != ingress.work.thread_id
            or arguments.get("reply_to", "") not in ("", ingress.work.reply_to)
        )
    ):
        return False

    try:

        def admitted(db: sqlite3.Connection) -> bool:
            # Recheck ownership and active membership in the same read as any
            # ingress proof. Never substitute current chat attachment here.
            with closing(
                db.execute(
                    "SELECT 1 FROM ngn_web_session_owners o JOIN harness_sessions h ON h.id = o.session_id "
                    "JOIN v2_sessions s ON s.id = h.id WHERE o.session_id = ? AND o.channel = ? "
                    "AND o.conversation_id = ? AND o.conflicted = 0",
                    (run.session_id, channel, owner.conversation_id),
                )
            ) as cursor:
                if cursor.fetchone() is None:
                    return False
            if ingress is None:
                return True
            work = ingress.work
            # Verify the exact running journal row and its committed history
            # identity, not a source dictionary or message-authored envelope.
            with closing(
                db.execute(
                    "SELECT 1 FROM ngn_web_inbox i "
                    "JOIN harness_sessions h ON h.id = i.session_id JOIN v2_sessions s ON s.id = h.id "
                    "JOIN ngn_web_message_origins o ON o.inbox_id = i.id "
                    "JOIN v2_messages m ON m.id = o.history_id AND m.session_id = i.session_id "
                    "WHERE i.id = ? AND i.session_id = ? AND i.channel = ? AND i.message_id = ? "
                    "AND i.conversation_id = ? AND i.thread_id = ? AND i.reply_to = ? "
                    "AND i.status = 'running' AND i.acknowledgement = ''",
                    (
                        work.id,
                        work.session_id,
                        work.channel,
                        work.message_id,
                        work.conversation_id,
                        work.thread_id,
                        work.reply_to,
                    ),
                )
            ) as cursor:
                return cursor.fetchone() is not None

        admitted_work = await host.store._transaction(admitted)
    except Exception:
        return False  # DB/ownership failures never become permission grants.
    return admitted_work and eligible()
