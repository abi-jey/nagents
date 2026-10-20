"""Narrow live limit updates using the web host's existing scoped settings.

This path never reconfigures a provider or replaces a running task tree. The
same host mutation guard serializes it with direct edits in Settings.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from typing import TYPE_CHECKING

import aiosqlite

from nagents.harness.subagent_settings import LIMIT_FIELDS
from nagents.harness.subagent_settings import LimitScope
from nagents.harness.subagent_settings import SubagentLimits
from nagents.harness.subagent_settings import SubagentSettingsSnapshot
from nagents.harness.subagent_settings import limit_scope

from ._async import join_owned
from .settings import MAX_SETTINGS_BYTES
from .settings import _GLOBAL_SETTINGS_TABLE
from .settings import SettingsValues

if TYPE_CHECKING:
    from .service import WebState

logger = logging.getLogger(__name__)


class WebSubagentSettings:
    def __init__(self, state: WebState) -> None:
        self.state = state
        self._reads = asyncio.Lock()

    def snapshot(self, scope: LimitScope) -> SubagentSettingsSnapshot:
        limit_scope(scope)
        if not hasattr(self.state, "settings"):
            limits = SubagentLimits.from_config(self.state.harness.config)
            return SubagentSettingsSnapshot(scope, "0" * 64, limits, limits)
        settings = self.state.settings
        values = settings.defaults if scope == "global" else settings.values
        limits = SubagentLimits(**{name: getattr(values, name) for name in LIMIT_FIELDS})
        effective = SubagentLimits(**{name: getattr(settings.values, name) for name in LIMIT_FIELDS})
        parts = [settings.global_revision]
        if scope == "workspace":
            parts.append(settings.revision)
        # Include effective values and full settings/document revisions: any
        # intervening user edit invalidates a pending concrete proposal.
        parts.extend((settings._settings_revision(scope), json.dumps(limits.values(), sort_keys=True)))
        return SubagentSettingsSnapshot(scope, hashlib.sha256(":".join(parts).encode()).hexdigest(), limits, effective)

    async def read(self, scope: LimitScope) -> SubagentSettingsSnapshot:
        async with self._reads:
            if hasattr(self.state, "settings") and not self.state.mutating:
                settings = self.state.settings
                await settings.load_global()
                # Global preferences are shared with other workspaces/processes.
                # Refresh only limits in admitted runs, never their provider or
                # permissions. Workspace overrides retain their precedence.
                inherited = {
                    name: settings.workspace_overrides.get(name, getattr(settings.defaults, name))
                    for name in LIMIT_FIELDS
                }
                settings.values = SettingsValues.model_validate({**settings.values.model_dump(), **inherited})
            return self.snapshot(scope)

    async def save(self, scope: LimitScope, revision: str, limits: SubagentLimits) -> SubagentSettingsSnapshot:
        async with self.state.queued_mutation(), self._reads:
            return await join_owned(asyncio.create_task(self._save(scope, revision, limits)))

    async def _save(self, scope: LimitScope, revision: str, limits: SubagentLimits) -> SubagentSettingsSnapshot:
        before = self.snapshot(scope)
        if before.revision != revision:
            raise ValueError("Settings changed while awaiting approval. Read the current settings and propose again.")
        settings = self.state.settings
        global_scope = scope == "global"
        path = settings.global_path if global_scope else settings.harness.agent.session.db_path
        table = "ngn_web_global_settings" if global_scope else "ngn_web_settings"
        expected = settings.global_revision if global_scope else settings.stored_revision if settings.persisted else ""
        absent = "0" * 64 if global_scope else ""
        next_revision = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        async with aiosqlite.connect(path, timeout=5) as db:
            if global_scope:
                await db.execute(_GLOBAL_SETTINGS_TABLE)
                await db.commit()
            else:
                # BEGIN IMMEDIATE covers both attached databases, binding an
                # inherited global revision until this workspace commit ends.
                settings.global_path.parent.mkdir(parents=True, exist_ok=True)
                settings.global_path.touch(mode=0o600, exist_ok=True)
                await db.execute("ATTACH DATABASE ? AS subagent_global", (str(settings.global_path),))
            await db.execute("BEGIN IMMEDIATE")
            if not global_scope:
                global_revision = "0" * 64
                async with db.execute(
                    "SELECT 1 FROM subagent_global.sqlite_master WHERE type='table' AND name='ngn_web_global_settings'"
                ) as cursor:
                    global_table = await cursor.fetchone()
                if global_table:
                    async with db.execute(
                        "SELECT revision FROM subagent_global.ngn_web_global_settings WHERE id = 1"
                    ) as cursor:
                        global_row = await cursor.fetchone()
                    global_revision = str(global_row[0]) if global_row else global_revision
                if global_revision != settings.global_revision:
                    raise ValueError(
                        "Global settings changed while awaiting approval. Read the current settings and propose again."
                    )
            # Table names come exclusively from the fixed host scope above.
            async with db.execute(
                f"SELECT version, CASE WHEN length(CAST(values_json AS BLOB)) <= ? THEN values_json END, revision "
                f"FROM {table} WHERE id = 1",
                (MAX_SETTINGS_BYTES,),
            ) as cursor:
                row = await cursor.fetchone()
            if (row[2] if row else absent) != expected:
                raise ValueError("Stored settings changed. Refresh Settings before proposing again.")
            if row and (row[0] != (1 if global_scope else 5) or not isinstance(row[1], str)):
                raise ValueError("Invalid saved settings")
            payload = json.loads(row[1]) if row else {}
            if not isinstance(payload, dict):
                raise ValueError("Invalid saved settings")
            for name, value in limits.values().items():
                if not global_scope and value == getattr(settings.defaults, name):
                    payload.pop(name, None)
                else:
                    payload[name] = value
            # Validate the full merged object while preserving unrelated saved
            # overrides. No model documents, credentials or tool policy writes.
            base = settings.startup_defaults if global_scope else settings.defaults
            SettingsValues.model_validate({**base.model_dump(), **payload})
            if self.snapshot(scope).revision != revision:
                raise ValueError("Settings changed while saving. Read the current settings and propose again.")
            await db.execute(
                f"INSERT INTO {table} (id, version, values_json, revision) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version, values_json=excluded.values_json, revision=excluded.revision",
                (1 if global_scope else 5, json.dumps(payload, separators=(",", ":")), next_revision),
            )
            await db.commit()
        # Commit and publication are cancellation-owned and contain no await in
        # between. A cancelled caller cannot leave stale in-memory authority.
        if global_scope:
            settings.defaults = settings.defaults.model_copy(update=limits.values())
            settings.global_revision = next_revision
            settings.global_persisted = True
            inherited = {name: settings.workspace_overrides.get(name, value) for name, value in limits.values().items()}
            settings.values = settings.values.model_copy(update=inherited)
            settings.revision = secrets.token_hex(32)
        else:
            settings.values = settings.values.model_copy(update=limits.values())
            settings.workspace_overrides = payload
            settings.revision = settings.stored_revision = next_revision
            settings.persisted = True
        logger.info(
            "Subagent settings saved: scope=%s limits=%s apply_at=next_model_turn revision=%s",
            scope,
            limits.values(),
            next_revision,
        )
        return self.snapshot(scope)
