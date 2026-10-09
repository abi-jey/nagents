"""Workspace-scoped, exact-definition approvals owned by the web operator.

These grants only answer the existing Harness approval callback. Tool enablement,
argument validation, reviewer ceilings and execution checks remain authoritative.
The private workspace database is separate from model-editable tools.yaml.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import ref

if TYPE_CHECKING:
    from weakref import ReferenceType

    from nagents.harness import Harness
    from nagents.harness.types import ApprovalRequest
    from nagents.types import ToolDefinition


@dataclass(frozen=True)
class ToolBinding:
    name: str
    key: str
    persistent: bool


class ToolApprovals:
    def __init__(self, path: Path, workspace: Path) -> None:
        self.path = path
        self.workspace = str(workspace.resolve())
        self._registrations: dict[int, tuple[ReferenceType[ToolDefinition], ReferenceType[object], str, str]] = {}

    def initialize(self) -> None:
        # The Harness creates this private parent directory before initialization.
        with closing(sqlite3.connect(self.path, timeout=0.2)) as db, db:
            self.path.chmod(0o600)
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_web_tool_approvals ("
                "workspace TEXT NOT NULL, tool TEXT NOT NULL, binding TEXT NOT NULL, "
                "persistent INTEGER NOT NULL, revision TEXT NOT NULL, "
                "PRIMARY KEY (workspace, tool))"
            )

    def binding(self, harness: Harness, name: str) -> ToolBinding | None:
        # A model response may already have installed the next generation. The
        # operator approves the exact definition advertised for this invocation,
        # never a same-name replacement waiting for the next model request.
        definition = harness.resources.definition(name)
        if definition is None or definition.func is None or not harness.resources.unchanged(name, definition):
            return None
        function = definition.func
        schema = json.dumps(definition.parameters, sort_keys=True, separators=(",", ":"))
        builtin = function == harness.tools.builtins.get(name)
        implementation = function.__func__ if inspect.ismethod(function) else function
        # Builtin methods have framework-owned receivers. Arbitrary extension
        # receivers/closures can hide a server, destination or mutable authority:
        # never transfer their grant to a newly registered same-name wrapper.
        captured = harness.resources.approval_identity(definition)
        if captured:
            # This identity describes the source/config loaded for this exact
            # callable, even if disk and the ready generation have since changed.
            key = hashlib.sha256(f"resource:{captured}\n{name}\n{schema}".encode()).hexdigest()
            return ToolBinding(name, key, True)
        stable = builtin or (inspect.isfunction(function) and not function.__closure__)
        if stable:
            try:
                source = inspect.getsource(implementation)
                filename = inspect.getsourcefile(implementation)
                if not filename:
                    raise ValueError("Tool has no inspectable source")
                origin = (
                    f"builtin:{implementation.__module__}:{implementation.__qualname__}"
                    if builtin
                    else f"extension:{Path(filename).resolve()}:{implementation.__qualname__}"
                )
                defaults = json.dumps(
                    [getattr(implementation, "__defaults__", None), getattr(implementation, "__kwdefaults__", None)],
                    sort_keys=True,
                )
                # Include the complete extension module: shared helpers/config
                # may change even when this particular function's body does not.
                if not builtin:
                    source = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
                key = hashlib.sha256(f"{origin}\n{name}\n{schema}\n{defaults}\n{source}".encode()).hexdigest()
                return ToolBinding(name, key, True)
            except (OSError, TypeError, ValueError):
                pass
        existing = self._registrations.get(id(definition))
        if (
            existing is None
            or existing[0]() is not definition
            or existing[1]() is not function
            or existing[2] != schema
        ):
            identifier = id(definition)

            def discarded(reference: ReferenceType[ToolDefinition]) -> None:
                current = self._registrations.get(identifier)
                if current is not None and current[0] is reference:
                    self._registrations.pop(identifier, None)

            try:
                existing = (ref(definition, discarded), ref(function), schema, secrets.token_hex(32))
            except TypeError:
                # An opaque, non-weak-referenceable callable cannot safely retain
                # registration identity without retaining arbitrary host state.
                return None
            self._registrations[identifier] = existing
        return ToolBinding(name, existing[3], False)

    def requested_binding(self, harness: Harness, request: ApprovalRequest) -> ToolBinding | None:
        if harness._closed:
            return None
        if request.task_id:
            root = harness.tasks.root
            child = root._children.get(request.task_id)
            info = root._infos.get(request.task_id)
            worker = root._workers.get(request.task_id)
            if (
                child is None
                or child._activation != request.activation
                or child.subagent_depth != request.depth
                or info is None
                or info.status != "running"
                or info.activation != request.activation
                or info.depth != request.depth
                or info.session_id != root.harness.session_id
                or worker is None
                or worker.done()
                or worker.cancelling()
            ):
                return None
            harness = child
        # Pending is published before the approval callback waits on its answer.
        # A child can be cancelled during publication/notices while the root run
        # and answer future still look live. Never save a rule for that stale call.
        if harness._closed or (
            harness._worker is not None and (harness._worker.done() or harness._worker.cancelling())
        ):
            return None
        if harness.config.demo or harness.mode == "reviewer" or harness.workspace.resolve() != Path(self.workspace):
            return None
        harness.tool_settings.load()
        if not harness.tool_settings.enabled(harness.config.agent, request.tool):
            return None
        return self.binding(harness, request.tool)

    def allowed(self, binding: ToolBinding) -> bool:
        with closing(sqlite3.connect(self.path, timeout=0.2)) as db:
            row = db.execute(
                "SELECT binding FROM ngn_web_tool_approvals WHERE workspace = ? AND tool = ?",
                (self.workspace, binding.name),
            ).fetchone()
        return row is not None and row[0] == binding.key

    def grant(self, binding: ToolBinding) -> None:
        # No await between the HTTP route's live nonce validation, commit and
        # future resolution. A cancellation/timeout cannot save a stale grant.
        # Lock contention fails quickly and leaves the approval undecided.
        with closing(sqlite3.connect(self.path, timeout=0.2)) as db, db:
            db.execute(
                "INSERT INTO ngn_web_tool_approvals (workspace, tool, binding, persistent, revision) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (workspace, tool) DO UPDATE SET "
                "binding=excluded.binding, persistent=excluded.persistent, revision=excluded.revision",
                (self.workspace, binding.name, binding.key, int(binding.persistent), secrets.token_hex(16)),
            )

    def snapshot(self) -> list[dict[str, object]]:
        with closing(sqlite3.connect(self.path, timeout=0.2)) as db:
            rows = db.execute(
                "SELECT tool, persistent, revision FROM ngn_web_tool_approvals WHERE workspace = ? ORDER BY tool",
                (self.workspace,),
            ).fetchall()
        return [
            {"tool": name, "persistent": bool(persistent), "revision": revision} for name, persistent, revision in rows
        ]

    def revoke(self, name: str, revision: str) -> bool:
        with closing(sqlite3.connect(self.path, timeout=0.2)) as db, db:
            cursor = db.execute(
                "DELETE FROM ngn_web_tool_approvals WHERE workspace = ? AND tool = ? AND revision = ?",
                (self.workspace, name, revision),
            )
            return cursor.rowcount == 1
