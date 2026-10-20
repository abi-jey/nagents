"""Revisioned delegation limits, separate from task accounting and permissions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import asdict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal
from typing import Protocol

from nagents._async import finish_on_cancel

if TYPE_CHECKING:
    from .config import HarnessConfig

LimitScope = Literal["global", "workspace"]
LIMIT_FIELDS = ("max_concurrent_subagents", "max_subagent_executions", "max_subagent_depth")
LIMIT_RANGES = ((1, 64), (1, 4096), (0, 8))


@dataclass(frozen=True)
class SubagentLimits:
    max_concurrent_subagents: int = 3
    max_subagent_executions: int = 8
    max_subagent_depth: int = 2

    def __post_init__(self) -> None:
        for name, (minimum, maximum) in zip(LIMIT_FIELDS, LIMIT_RANGES, strict=True):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")

    @classmethod
    def from_config(cls, config: HarnessConfig) -> SubagentLimits:
        return cls(**{name: getattr(config, name) for name in LIMIT_FIELDS})

    def values(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class SubagentSettingsSnapshot:
    scope: LimitScope
    revision: str
    limits: SubagentLimits
    effective: SubagentLimits

    def values(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "revision": self.revision,
            "limits": self.limits.values(),
            "effective": self.effective.values(),
            "apply_at": "next_model_turn",
            "supported_ranges": {
                name: {"minimum": minimum, "maximum": maximum}
                for name, (minimum, maximum) in zip(LIMIT_FIELDS, LIMIT_RANGES, strict=True)
            },
        }


class SubagentSettingsHost(Protocol):
    """A host owns persistence; a task tree owns accounting and accepted turns."""

    async def read(self, scope: LimitScope) -> SubagentSettingsSnapshot: ...

    async def save(self, scope: LimitScope, revision: str, limits: SubagentLimits) -> SubagentSettingsSnapshot: ...


class LocalSubagentSettings:
    """Portable global/workspace preferences for terminal and embedded harnesses.

    The web host supplies its own adapter to its existing scoped Settings store.
    Neither adapter is allowed to write provider, tool-policy, or credential data.
    """

    def __init__(self, config: HarnessConfig) -> None:
        self.path = config.data_dir / "subagent-settings.db"
        self.workspace = hashlib.sha256(str(config.workspace).encode()).hexdigest()
        self.defaults = SubagentLimits.from_config(config)
        self.config_paths = config.config_paths
        self.environment_overrides = dict(config.subagent_limit_environment)
        self.explicit_overrides = dict(config.subagent_limit_overrides)
        if config.config_paths:
            has_load_snapshot = bool(config.subagent_limit_file_values)
            if has_load_snapshot:
                file_values = config.subagent_limit_file_values
                file_fields = set(config.subagent_limit_file_fields)
            else:
                try:
                    file_defaults, file_fields = self._read_config_files()
                    file_values = file_defaults.values()
                except (ValueError, OSError):
                    # Preserve the established lazy validation boundary: read()
                    # reports malformed trusted files, not adapter construction.
                    file_values = SubagentLimits().values()
                    file_fields = set()
            builtin_values = SubagentLimits().values()
            self.direct_overrides = {
                name: getattr(config, name)
                for name in LIMIT_FIELDS
                if getattr(config, name) != file_values[name]
                and (
                    (
                        has_load_snapshot
                        and (name in file_fields or getattr(config, name) != self.environment_overrides.get(name))
                    )
                    or (not has_load_snapshot and getattr(config, name) != builtin_values[name])
                )
                and name not in self.explicit_overrides
            }
        else:
            self.direct_overrides = {}

    def _defaults(self) -> SubagentLimits:
        if self.config_paths:
            defaults, configured_fields = self._read_config_files()
            values = defaults.values()
        else:
            values = self.defaults.values()
            configured_fields = set()
        for name, value in self.environment_overrides.items():
            if name not in configured_fields:
                values[name] = value
        values.update(self.explicit_overrides)
        values.update(self.direct_overrides)
        return SubagentLimits(**values)

    def _read_config_files(self) -> tuple[SubagentLimits, set[str]]:
        values = SubagentLimits().values()
        configured_fields: set[str] = set()
        for path in self.config_paths:
            try:
                with path.open(encoding="utf-8") as source:
                    text = source.read(1_048_577)
            except FileNotFoundError:
                continue
            if len(text.encode()) > 1_048_576:
                raise ValueError("Trusted configuration exceeds its size limit")
            document = json.loads(text)
            if not isinstance(document, dict):
                raise ValueError("Trusted configuration must contain an object")
            selected = {name: document[name] for name in LIMIT_FIELDS if name in document}
            values.update(selected)
            configured_fields.update(selected)
            SubagentLimits(**values)
        return SubagentLimits(**values), configured_fields

    def _key(self, scope: LimitScope) -> str:
        if scope not in ("global", "workspace"):
            raise ValueError("Settings scope must be global or workspace")
        return "global" if scope == "global" else self.workspace

    def _rows(self, db: sqlite3.Connection) -> dict[str, tuple[SubagentLimits, str]]:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ngn_subagent_settings'").fetchone():
            return {}
        rows = db.execute(
            "SELECT scope, CASE WHEN length(CAST(values_json AS BLOB)) <= 1024 THEN values_json END, revision "
            "FROM ngn_subagent_settings WHERE scope IN ('global', ?)",
            (self.workspace,),
        ).fetchall()
        result: dict[str, tuple[SubagentLimits, str]] = {}
        for scope, payload, revision in rows:
            if not isinstance(payload, str) or not isinstance(revision, str) or len(revision) != 64:
                raise ValueError("Invalid saved subagent settings")
            values = json.loads(payload)
            if not isinstance(values, dict) or set(values) != set(LIMIT_FIELDS):
                raise ValueError("Invalid saved subagent limits")
            result[scope] = SubagentLimits(**values), revision
        return result

    def _snapshot(
        self, scope: LimitScope, rows: dict[str, tuple[SubagentLimits, str]], defaults: SubagentLimits
    ) -> SubagentSettingsSnapshot:
        global_limits, global_revision = rows.get("global", (defaults, "0" * 64))
        local_limits, local_revision = rows.get(self.workspace, (global_limits, "0" * 64))
        selected = global_limits if scope == "global" else local_limits
        # Workspace approvals also bind the inherited global revision. Defaults
        # are hashed, so editing a trusted config invalidates an older proposal.
        parts = [json.dumps(defaults.values(), sort_keys=True), global_revision]
        if scope == "workspace":
            parts.append(local_revision)
        revision = hashlib.sha256(":".join(parts).encode()).hexdigest()
        return SubagentSettingsSnapshot(scope, revision, selected, local_limits)

    async def read(self, scope: LimitScope) -> SubagentSettingsSnapshot:
        self._key(scope)

        def read() -> SubagentSettingsSnapshot:
            defaults = self._defaults()
            if not self.path.exists():
                return self._snapshot(scope, {}, defaults)
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                db.execute("BEGIN")
                return self._snapshot(scope, self._rows(db), defaults)

        return await finish_on_cancel(asyncio.to_thread(read))

    async def save(self, scope: LimitScope, revision: str, limits: SubagentLimits) -> SubagentSettingsSnapshot:
        key = self._key(scope)

        def save() -> SubagentSettingsSnapshot:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ngn_subagent_settings ("
                    "scope TEXT PRIMARY KEY, values_json TEXT NOT NULL, revision TEXT NOT NULL)"
                )
                db.execute("BEGIN IMMEDIATE")
                defaults = self._defaults()
                rows = self._rows(db)
                if self._snapshot(scope, rows, defaults).revision != revision:
                    raise ValueError("Subagent settings changed. Read the current settings before proposing again.")
                saved_revision = secrets.token_hex(32)
                db.execute(
                    "INSERT INTO ngn_subagent_settings VALUES (?, ?, ?) ON CONFLICT(scope) DO UPDATE SET "
                    "values_json=excluded.values_json, revision=excluded.revision",
                    (key, json.dumps(limits.values(), separators=(",", ":")), saved_revision),
                )
                rows[key] = limits, saved_revision
                return self._snapshot(scope, rows, defaults)

        return await finish_on_cancel(asyncio.to_thread(save))


def limit_scope(value: str) -> LimitScope:
    if value not in ("global", "workspace"):
        raise ValueError("Settings scope must be global or workspace")
    return "global" if value == "global" else "workspace"
