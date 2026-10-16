"""Authenticated task inspection and human messages through owned root runtimes."""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import asdict
from typing import TYPE_CHECKING
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Path as PathParameter
from fastapi import Query
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from nagents.harness.followups import RunInput
from nagents.harness.reasoning import ReasoningPreference
from nagents.harness.reasoning import resolve_effort
from nagents.harness.runtime import _HarnessSession
from nagents.harness.task_activity import TaskOptions
from nagents.session import response_metadata

from ._async import finish_on_cancel
from .task_history import TaskHistory
from .task_store import TaskStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable

    from nagents.harness.runtime import Harness

    from .service import Run
    from .service import WebState

RootId = Annotated[str, PathParameter(min_length=1, max_length=80, pattern=r"^ngn-[a-zA-Z0-9-]+$")]
TaskId = Annotated[str, PathParameter(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
TASK_DRAIN_TIMEOUT = 5.0


class TaskMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    message_id: str = Field(min_length=1, max_length=36)
    text: str = Field(min_length=1, max_length=16000)

    @field_validator("message_id")
    @classmethod
    def identifier(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value


class TaskStartInput(TaskMessageInput):
    agent: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class TaskSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=200)
    compact_trigger: str = Field(default="", pattern=r"^(|auto|tokens|messages|off)$")
    compact_tokens: int = Field(default=200000, ge=1024, le=10000000)
    compact_messages: int = Field(default=100, ge=1, le=10000)
    reasoning_effort: ReasoningPreference = ""


class Tasks:
    def __init__(self, state: WebState) -> None:
        self.state = state
        self.store = TaskStore(state.history.db_path, secrets.token_hex(16))
        self.admission = asyncio.Lock()
        self.activity: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.cursors: dict[tuple[str, str], int] = {}
        self.boundaries: dict[tuple[str, str], int] = {}
        self.truncated: set[tuple[str, str]] = set()
        self.created: dict[str, asyncio.Event] = {}

    def forget_session(self, root: str) -> None:
        """Drop transient inspection data after all producers have joined."""
        for cache in (self.activity, self.cursors, self.boundaries):
            for key in tuple(cache):
                if key[0] == root:
                    del cache[key]
        self.truncated = {key for key in self.truncated if key[0] != root}

    def owner(self, root: str) -> Harness:
        # The configuration harness fallback is deliberately not authority.
        owner = self.state.executions.owners.get(root)
        run = self.state.run_for(root)
        if run is not None and run.harness is not None:
            owner = run.harness
        if owner is None or owner.session_id != root or owner._closed:
            raise HTTPException(409, "This task runtime is unavailable. Start a new agent.")
        return owner

    async def index(self, root: str) -> dict[str, object]:
        await self.store.validate(root)
        self.state.harness.refresh_agent_definitions()
        saved = {str(task["id"]): task for task in await self.store.tasks(root)}
        for task in saved.values():
            task.update(
                can_message=False, unavailable_reason="This task runtime is unavailable; its history is read-only."
            )
        owner: Harness | None
        try:
            owner = self.owner(root)
        except HTTPException:
            owner = None
        if owner is not None:
            for info in owner.tasks.list():
                task = self.task(asdict(info), owner)
                saved[info.id] = task
        if (
            self.state.session_agents.unavailable(self.state.session_agents.selected(root))
            and self.state.run_for(root) is None
        ):
            # Keep saved conversations inspectable after an administrator removes
            # a profile; the main picker can select a replacement before work.
            return {"tasks": list(saved.values()), "profiles": []}
        async with self.state.session_agents.view(root) as profile_owner:
            used = {task.get("profile") for task in saved.values()} | {profile_owner.config.agent}
            profiles: list[dict[str, object]] = []
            for name in profile_owner.config.profile_names:
                profile = profile_owner.config.profile(name)
                unavailable = ""
                try:
                    binding = profile_owner.resolved_model_binding(name).binding
                    provider_id, model_id = binding.provider, binding.id
                except ValueError:
                    provider_id, model_id = profile.provider, profile.model
                    unavailable = (
                        "The agent's model reference is unavailable. Open its model settings to choose a connection."
                    )
                available = (
                    profile.user_invocable
                    and profile_owner.can_delegate
                    and name in profile_owner.delegatable_profile_names
                    and not unavailable
                )
                profiles.append(
                    {
                        "id": name,
                        "name": name,
                        "description": profile.description[:2000],
                        "provider": provider_id
                        or profile_owner.config.provider
                        or profile_owner.config.provider_profile().kind,
                        "model": model_id or profile_owner.agent.provider.model,
                        "reasoning_effort": resolve_effort(profile.reasoning_effort, profile_owner.reasoning_effort()),
                        "source": profile.source,
                        "used": name in used,
                        "available": available,
                        "can_start": available,
                        "unavailable_reason": ""
                        if available
                        else unavailable or "This profile is not available for delegation from the current agent.",
                    }
                )
            return {
                "tasks": list(saved.values()),
                "profiles": profiles,
                "profile_revision": profile_owner.profile_revision,
            }

    @staticmethod
    def task(data: dict[str, object], owner: Harness) -> dict[str, object]:
        task_id = str(data["id"])
        child = owner.tasks._children.get(task_id)
        can_message = (
            owner.allow_subagents
            and data.get("status") != "cancelled"
            and (data.get("status") == "running" or (child is not None and child._initialized))
        )
        return {**data, "can_message": can_message, "unavailable_reason": "" if can_message else "Task is unavailable."}

    async def record(self, run: Run, record: dict[str, object]) -> None:
        kind = record.get("event")
        if kind not in {"task_started", "task_completed", "task_message", "task_activity"}:
            return
        identifier = str(record.get("task_id", ""))
        try:
            owner = self.owner(run.session_id)
            info = owner.tasks._lookup(identifier)
        except (HTTPException, ValueError):
            return
        task = self.task(asdict(info), owner)
        if kind != "task_activity":
            await self.store.save(run.session_id, task)
        ready = self.created.get(run.task_message_id)
        if kind == "task_started" and ready is not None and not ready.is_set() and not info.parent_task_id:
            await self.store.bind(run.task_message_id, {**task, "receipt_run_id": run.id})
            # The input context can finish binding first and release the HTTP
            # waiter, which then removes this registry entry during our await.
            # We own this exact Event, not a later lookup in that registry.
            ready.set()
        key = (run.session_id, identifier)
        current_activation = record.get("activation", info.activation) == info.activation
        if kind == "task_started" and current_activation:
            self.activity[key] = []
        if kind == "task_completed" and current_activation:
            self.activity.pop(key, None)
            self.truncated.discard(key)
            for receipt in await self.store.fail_task(run.session_id, identifier, info.activation):
                self.publish(receipt)
        elif kind == "task_activity" and current_activation:
            sequence = record.get("sequence", 0)
            if type(sequence) is int:
                self.cursors[key] = sequence
            payload = record.get("payload", {})
            if not isinstance(payload, dict):
                return
            event = payload.get("type")
            events = self.activity.setdefault(key, [])
            # Keep only the in-flight response. Persisted messages are returned
            # separately; neither stream is expanded into the root chat.
            if event == "history_committed":
                events.clear()
                history_id = payload.get("history_id")
                if type(history_id) is int and history_id >= 0:
                    self.boundaries[key] = history_id
                self.truncated.discard(key)
            elif event == "resync_required":
                events.clear()
                self.truncated.add(key)
            elif event in {
                "text_chunk",
                "text_done",
                "response_metadata",
                "tool_call_progress",
                "tool_call",
                "tool_execution_started",
                "tool_output",
                "tool_result",
            }:
                if event == "response_metadata":
                    events[:] = [
                        old
                        for old in events
                        if not (
                            isinstance(previous_metadata := old.get("payload"), dict)
                            and previous_metadata.get("type") == event
                            and previous_metadata.get("response_id") == payload.get("response_id")
                        )
                    ]
                copied = json.loads(json.dumps(record))
                if events and event in {"text_chunk", "tool_output"}:
                    old = events[-1]
                    previous = old.get("payload", {})
                    if (
                        isinstance(previous, dict)
                        and previous.get("type") == event
                        and all(old.get(field) == record.get(field) for field in ("activation", "turn_id"))
                        and all(
                            previous.get(field) == payload.get(field)
                            for field in ("generation_id", "index", "id", "name")
                        )
                    ):
                        field = "chunk" if event == "text_chunk" else "text"
                        combined = str(previous.get(field, "")) + str(payload.get(field, ""))
                        copied["payload"][field] = combined[-32768:]
                        if len(combined) > 32768:
                            copied["payload"]["truncated"] = True
                            self.truncated.add(key)
                        events.pop()
                events.append(copied)
                while len(events) > 128 or len(json.dumps(events)) > 131072:
                    events.pop(0)
                    self.truncated.add(key)

    def publish(self, receipt: dict[str, object]) -> None:
        record = {"event": "task_message_receipt", "task_id": receipt["task_id"], "receipt": receipt}
        run = self.state.run_for(str(receipt["session_id"]))
        if run is not None:
            self.state.publish(run, record)
        else:
            self.state.bus.event({**record, "session_id": receipt["session_id"], "schema_version": 1})

    @asynccontextmanager
    async def input_context(self, owner: Harness, identifier: str) -> AsyncIterator[None]:
        info, child = owner.tasks.input_child(identifier)
        task = self.task(asdict(info), owner)
        await self.store.save(info.session_id, task)
        await self.store.bind(identifier, self.receipt_target(info.session_id, task))
        if ready := self.created.get(identifier):
            ready.set()
        previous = child.agent.session
        if type(previous) is not _HarnessSession:
            raise RuntimeError("This child persistence adapter does not support verified web inputs")
        adapter = TaskHistory(self.store, identifier, info.child_session_id, self.publish)
        child.agent.session = adapter
        child.agent.plugins.append(adapter.identity)
        try:
            yield
        finally:
            child.agent.plugins[:] = [plugin for plugin in child.agent.plugins if plugin is not adapter.identity]
            if child.agent.session is adapter:
                child.agent.session = previous

    def message(self, owner: Harness, body: TaskMessageInput) -> RunInput:
        return RunInput(
            body.text,
            identifier=body.message_id,
            message_id=body.message_id,
            kind="human",
            context=lambda: self.input_context(owner, body.message_id),
        )

    def receipt_target(self, root: str, task: dict[str, object]) -> dict[str, object]:
        run = self.state.run_for(root)
        if run is None or run.finished:
            raise RuntimeError("Task input has no active web execution owner")
        return {**task, "receipt_run_id": run.id}

    def draining(self, root: str, owner: Harness) -> Run | None:
        run = self.state.run_for(root)
        if run is None:
            return None
        if run.task.cancelling() or run.outcome == "cancelled":
            raise HTTPException(409, "This chat is stopping. This message was not admitted.")
        worker = owner._worker
        if run.draining or (worker is not None and worker.done() and not owner.tasks._active):
            return run
        return None

    async def wait_for_drain(self, run: Run, deadline: float) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise HTTPException(409, "This chat is still finishing. Retry after it finishes.")
        done, _ = await asyncio.wait({run.task}, timeout=remaining)
        if not done:
            raise HTTPException(409, "This chat is still finishing. Retry after it finishes.")
        if run.outcome == "cancelled" or run.task.cancelled():
            raise HTTPException(409, "This chat was stopped. This message was not admitted.")

    def finishing_claim(self, root: str) -> asyncio.Task[None] | None:
        if self.state.run_for(root) is not None:
            return None
        # Claim startup reserves a Run before publishing work_tasks. Its absence
        # therefore identifies only post-run claim bookkeeping, not a future
        # model run. Never join a work_tasks entry while it still owns a Run.
        task = self.state.channels.work_cleanup.get(root) or self.state.channels.work_tasks.get(root)
        if task is not None and (task.cancelling() or task.cancelled()):
            raise HTTPException(409, "This chat is stopping. This message was not admitted.")
        return task

    async def wait_for_claim(self, task: asyncio.Task[None], deadline: float) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        done, _ = await asyncio.wait({task}, timeout=max(0.0, remaining))
        if not done or task.cancelled():
            raise HTTPException(409, "This chat is still finishing. Retry after it finishes.")

    async def accept(self, root: str, task_id: str, body: TaskMessageInput, owner: Harness) -> dict[str, object]:
        message = self.message(owner, body)
        if owner.tasks.submit_message(task_id, message):
            accepted = asdict(owner.tasks._lookup(task_id))
        elif self.state.run_for(root) is not None:
            accepted = asdict(owner.tasks.continue_task(task_id, body.text, message=message))
        else:
            # The scheduled worker has not accepted its new activation yet.
            self.launch(root, body, task_id=task_id)
            return await self.store.duplicate(root, task_id, body.message_id, body.text)
        target = self.receipt_target(root, accepted)
        receipt = await self.store.bind(body.message_id, target)
        current = self.state.run_for(root)
        info = owner.tasks._lookup(task_id)
        if receipt["status"] == "queued" and (
            current is None
            or current.id != target["receipt_run_id"]
            or info.status != "running"
            or info.activation != receipt["activation"]
        ):
            receipt = await self.store.fail(body.message_id, "The agent ended before reading this message.")
        return receipt

    async def send(self, root: str, task_id: str, body: TaskMessageInput) -> dict[str, object]:
        journaled = False
        deadline = asyncio.get_running_loop().time() + TASK_DRAIN_TIMEOUT
        try:
            while True:
                async with self.admission:
                    if self.state.mutating or self.state.executions.closed:
                        raise HTTPException(409, "Server configuration is changing. Retry shortly.")
                    duplicate = await self.store.duplicate(root, task_id, body.message_id, body.text)
                    if duplicate and (not journaled or duplicate["status"] != "queued"):
                        return duplicate
                    known = (await self.index(root))["tasks"]
                    assert isinstance(known, list)
                    if not any(task["id"] == task_id for task in known):
                        raise HTTPException(404, "Task not found in this session.")
                    owner = self.owner(root)
                    info = owner.tasks._lookup(task_id)
                    if info.status == "cancelled":
                        raise HTTPException(409, "This agent was cancelled. Start a new agent.")
                    draining = self.draining(root, owner)
                    claim = self.finishing_claim(root)
                    if draining is None and claim is None:
                        if self.state.run_for(root) is not None and not owner.tasks._active:
                            raise HTTPException(409, "This chat is starting. Retry the message shortly.")
                        if not journaled:
                            await self.store.insert(root, task_id, body.message_id, body.text, task_id)
                            journaled = True
                        if self.owner(root) is not owner:
                            raise RuntimeError("Task owner changed")
                        # Core completion can race the journal transaction.
                        # Unbound receipts belong to this pending admission,
                        # never to the previous run's cleanup.
                        draining = self.draining(root, owner)
                        claim = self.finishing_claim(root)
                        if draining is None and claim is None:
                            receipt = await self.accept(root, task_id, body, owner)
                            self.publish(receipt)
                            return receipt
                # Joining a completed producer's web cleanup never holds the
                # workspace admission lock or cancels that prior execution.
                if draining is not None:
                    await self.wait_for_drain(draining, deadline)
                else:
                    assert claim is not None
                    await self.wait_for_claim(claim, deadline)
        except (ValueError, RuntimeError, PermissionError, HTTPException) as error:
            if journaled:
                receipt = await self.store.fail(
                    body.message_id,
                    "The agent could not accept this message. Start a new agent or retry with a new message ID.",
                )
                self.publish(receipt)
            if isinstance(error, HTTPException):
                raise
            raise HTTPException(409, "The agent could not accept this message. Retry after it finishes.") from None

    async def start(self, root: str, body: TaskStartInput) -> dict[str, object]:
        async with self.admission:
            target = f"profile:{body.agent}"
            duplicate = await self.store.duplicate(root, target, body.message_id, body.text)
            if duplicate:
                tasks = await self.store.tasks(root)
                return {
                    "receipt": duplicate,
                    "task": next((task for task in tasks if task["id"] == duplicate["task_id"]), {}),
                }
            if self.state.mutating or self.state.executions.closed:
                raise HTTPException(409, "Server configuration is changing. Retry shortly.")
            async with self.state.session_agents.view(root) as current:
                if (
                    body.agent not in current.delegatable_profile_names
                    or not current.config.profile(body.agent).user_invocable
                    or not current.can_delegate
                ):
                    raise HTTPException(403, "This agent profile cannot be started here.")
            await self.store.insert(root, target, body.message_id, body.text)
            ready = self.created[body.message_id] = asyncio.Event()
            try:
                if self.state.run_for(root) is not None:
                    owner = self.owner(root)
                    info = await owner.tasks.start_task(body.text, body.agent, message=self.message(owner, body))
                    await self.store.save(root, self.task(asdict(info), owner))
                    await self.store.bind(body.message_id, self.receipt_target(root, asdict(info)))
                    ready.set()
                else:
                    self.launch(root, body, agent=body.agent)
                async with asyncio.timeout(10):
                    await ready.wait()
            except TimeoutError:
                # A startup timeout is not cancellation: the owned run will
                # settle the receipt. Return its authoritative queued state.
                if not self.state.run_for(root):
                    self.publish(await self.store.fail(body.message_id, "Agent startup was unavailable."))
            except (ValueError, RuntimeError, PermissionError, HTTPException):
                self.publish(await self.store.fail(body.message_id, "Agent startup was unavailable."))
            finally:
                self.created.pop(body.message_id, None)
            receipt = await self.store.duplicate(root, target, body.message_id, body.text)
            tasks = await self.store.tasks(root)
            return {"receipt": receipt, "task": next((task for task in tasks if task["id"] == receipt["task_id"]), {})}

    async def configure(self, root: str, task_id: str, body: TaskSettingsInput) -> dict[str, object]:
        async with self.admission:
            if self.state.mutating or self.state.executions.closed:
                raise HTTPException(409, "Server configuration is changing. Retry shortly.")
            return await self._configure(root, task_id, body)

    async def _configure(self, root: str, task_id: str, body: TaskSettingsInput) -> dict[str, object]:
        known = (await self.index(root))["tasks"]
        assert isinstance(known, list)
        if not any(task["id"] == task_id for task in known):
            raise HTTPException(404, "Task not found in this session.")
        owner = self.owner(root)
        try:
            info = owner.tasks.configure_task(
                task_id,
                TaskOptions(
                    body.provider,
                    body.model,
                    body.compact_trigger,
                    body.compact_tokens,
                    body.compact_messages,
                    body.reasoning_effort,
                ),
            )
        except (ValueError, RuntimeError, PermissionError) as error:
            raise HTTPException(409, str(error)) from None
        task = self.task(asdict(info), owner)
        await self.store.save(root, task)
        record: dict[str, object] = {
            "event": "task_settings",
            "task_id": task_id,
            "task": task,
            "session_id": root,
            "schema_version": 1,
        }
        self.state.bus.event(record)
        return {"task": task}

    def launch(self, root: str, body: TaskMessageInput, *, task_id: str = "", agent: str = "") -> None:
        from .service import Run

        if root in self.state.channels.work_tasks or root in self.state.channels.work_cleanup:
            raise HTTPException(409, "This chat is finishing another operation. Retry shortly.")
        run = Run(root, server_owned=True, message_id=body.message_id, task_message_id=body.message_id)
        self.state.executions.reserve(run)

        async def execute() -> None:
            try:
                run.executing = True
                with self.state.executions.scope(run):
                    async with self.state.executions.harness(run) as owner:
                        self.state.running_harness = owner
                        await owner.resume(root)
                        await self.state.produce(
                            run, body.text, task_id=task_id, task_agent=agent, task_input=self.message(owner, body)
                        )
            except asyncio.CancelledError:
                run.outcome = "cancelled"
                raise
            except Exception:
                run.outcome = "failed"
            finally:
                receipt = await finish_on_cancel(
                    self.store.fail(body.message_id, "Agent execution ended before reading this message.")
                )
                self.publish(receipt)
                if ready := self.created.get(body.message_id):
                    ready.set()
                self.state.finish(run)
                await self.state.executions.release_idle(root)

        run.task = asyncio.create_task(execute(), name=f"ngn-task-input-{body.message_id}")
        self.state.publish(run, {"event": "run_started", "message_id": body.message_id, "task_id": task_id})
        self.state.status()

    async def finish(self, run: Run) -> None:
        await self.store.initialize()
        owner: Harness | None
        try:
            owner = self.owner(run.session_id)
        except HTTPException:
            owner = None
        if owner is not None:
            for info in owner.tasks.list():
                await self.store.save(run.session_id, self.task(asdict(info), owner))
        for key in tuple(self.activity):
            if key[0] == run.session_id:
                self.activity.pop(key, None)

        def unread(db: sqlite3.Connection) -> list[str]:
            return [
                str(row[0])
                for row in db.execute(
                    "SELECT message_id FROM ngn_web_task_inputs WHERE root = ? AND epoch = ? AND status = 'queued' "
                    "AND (owner_run_id = ? OR message_id = ?)",
                    (run.session_id, self.store.epoch, run.id, run.task_message_id),
                )
            ]

        for identifier in await self.store.db._transaction(unread):
            self.publish(await self.store.fail(identifier, "The agent stopped before reading this message."))

    async def detail(self, root: str, task_id: str, *, limit: int = 100, before: int = 0) -> dict[str, object]:
        async def snapshot(_: str) -> dict[str, object]:
            indexed = await self.index(root)
            tasks = indexed["tasks"]
            assert isinstance(tasks, list)
            task = next((item for item in tasks if item["id"] == task_id), None)
            if task is None:
                raise HTTPException(404, "Task not found in this session.")
            child = str(task["child_session_id"])
            key = (root, task_id)
            boundary = (
                self.boundaries.get(key, 0) if key in self.activity or task.get("status") == "running" else 2**63 - 1
            )
            # Freeze the evidence cut before any database await. Later stream
            # events are replayed after this exact task cursor; unrelated agents
            # need not become quiet before inspection can succeed.
            activity = json.loads(json.dumps(self.activity.get(key, [])))
            activity_cursor = self.cursors.get(key, 0)
            activity_truncated = key in self.truncated

            def read(db: sqlite3.Connection) -> tuple[list[dict[str, object]], bool]:
                self.store.root(db, root)
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT id, role, content, tool_calls, tool_call_id, name FROM v2_messages "
                    "WHERE session_id = ? AND (? = 0 OR id < ?) AND id <= ? AND id >= COALESCE("
                    "(SELECT compacted_at_message_id FROM v2_sessions WHERE id = ?), 0) "
                    "AND role NOT IN ('system', 'developer') ORDER BY id DESC LIMIT ?",
                    (child, before, before, boundary, child, limit + 1),
                ).fetchall()
                more = len(rows) > limit
                rows = rows[:limit]
                records: list[dict[str, object]] = []
                for row in reversed(rows):
                    content = row["content"] or ""
                    try:
                        raw_calls = json.loads(row["tool_calls"] or "[]")
                    except (ValueError, TypeError):
                        raw_calls = []
                    if not isinstance(raw_calls, list):
                        raw_calls = []
                    calls = [
                        {"id": call.get("id", ""), "name": call.get("name", ""), "arguments": call.get("arguments", {})}
                        for call in raw_calls
                        if isinstance(call, dict)
                    ]
                    record: dict[str, object] = {
                        "history_id": str(row["id"]),
                        "role": row["role"],
                        "content": content[:32768],
                        "tool_calls": calls,
                        "tool_call_id": row["tool_call_id"] or "",
                        "name": row["name"] or "",
                        "parts": [],
                        "source_verified": False,
                    }
                    if len(json.dumps(record)) > 65536:
                        record.update(tool_calls=[], content=content[:16000], truncated=True)
                    elif len(content) > 32768:
                        record["truncated"] = True
                    records.append(record)
                records = response_metadata.merge(db, child, records, orphans=not before, maximum=boundary)
                while len(json.dumps(records)) > 262144:
                    records.pop(0)
                    more = True
                return records, more

            history, more = await self.store.db._transaction(read)
            if boundary == 2**63 - 1:
                # A retained completed task may have been reactivated while
                # reading. Retry that transition rather than labeling its new
                # history with the old activation's cursor.
                try:
                    current = self.owner(root).tasks._lookup(task_id)
                except (HTTPException, ValueError):
                    pass
                else:
                    if current.activation != task.get("activation", 0):
                        return await snapshot(root)
            context: dict[str, object] = {}
            with suppress(HTTPException, ValueError, RuntimeError):
                context = await self.owner(root).tasks.context_stats(task_id)
            return {
                "task": task,
                "history": history,
                "activity": activity,
                "receipts": await self.store.receipts(root, task_id),
                "context": context,
                "activity_cursor": activity_cursor,
                "activity_truncated": activity_truncated,
                "history_boundary": boundary,
                "before": history[0]["history_id"] if more and history else "",
                "history_truncated": more or any(row.get("truncated") for row in history),
            }

        return await snapshot(root)


def register(app: FastAPI, state: Callable[[], WebState]) -> None:
    @app.get("/api/sessions/{session_id}/tasks")
    async def index(session_id: RootId) -> dict[str, object]:
        return await state().tasks.index(session_id)

    @app.get("/api/sessions/{session_id}/tasks/{task_id}")
    async def detail(
        session_id: RootId,
        task_id: TaskId,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        before: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    ) -> dict[str, object]:
        return await state().tasks.detail(session_id, task_id, limit=limit, before=before)

    @app.post("/api/sessions/{session_id}/tasks/{task_id}/messages")
    async def message(session_id: RootId, task_id: TaskId, body: TaskMessageInput) -> dict[str, object]:
        return await finish_on_cancel(state().tasks.send(session_id, task_id, body))

    @app.post("/api/sessions/{session_id}/tasks")
    async def start(session_id: RootId, body: TaskStartInput) -> dict[str, object]:
        return await finish_on_cancel(state().tasks.start(session_id, body))

    @app.put("/api/sessions/{session_id}/tasks/{task_id}/settings")
    async def settings(session_id: RootId, task_id: TaskId, body: TaskSettingsInput) -> dict[str, object]:
        return await finish_on_cancel(state().tasks.configure(session_id, task_id, body))
