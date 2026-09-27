"""Global voice defaults and per-workspace field overrides, independent of providers."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from typing import Literal
from typing import TypeVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from nagents.live import LiveConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from nagents.harness.providers import ScopedProviderRegistryStore

MAX_VOICE_BYTES = 4096
ModelId = str
Scope = Literal["global", "workspace"]
T = TypeVar("T")


class VoicePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    enabled: bool = False
    backend_mode: Literal["assistant", "hosted"] = "assistant"
    model: ModelId = Field(
        default="gpt-live-1", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$"
    )
    backend_model: ModelId = Field(
        default_factory=lambda: LiveConfig().backend_model,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$",
    )
    voice: Literal["marin", "cedar"] = "marin"


class VoiceOverrides(VoicePreferences):
    """Only explicitly supplied fields override the global value."""

    def selected(self) -> dict[str, str | bool]:
        return self.model_dump(exclude_unset=True)


class VoicePreferenceStore:
    """Both databases participate in one short SQLite transaction for CAS reads/writes."""

    def __init__(self, workspace_db: Path, providers: ScopedProviderRegistryStore) -> None:
        self.workspace_db = workspace_db
        self.providers = providers
        self.global_db = providers.global_store.path.parent / "voice.db"

    @staticmethod
    def _row(db: sqlite3.Connection, schema: str) -> tuple[VoicePreferences | VoiceOverrides, str]:
        with closing(
            db.execute(
                f"SELECT version, CASE WHEN typeof(payload) = 'text' AND length(CAST(payload AS BLOB)) <= ? "
                f"THEN payload END, revision FROM {schema}.ngn_voice_preferences WHERE id = 1",
                (MAX_VOICE_BYTES,),
            )
        ) as cursor:
            row = cursor.fetchone()
        if row is None or row[0] != 1 or not isinstance(row[1], str) or not isinstance(row[2], str):
            raise ValueError("Invalid Voice preferences")
        if not re.fullmatch(r"[0-9a-f]{64}", row[2]):
            raise ValueError("Invalid Voice revision")
        return (VoicePreferences if schema == "voice_global" else VoiceOverrides).model_validate_json(row[1]), row[2]

    def _initialize(self, db: sqlite3.Connection) -> None:
        for schema in ("voice_global", "main"):
            db.execute(
                f"CREATE TABLE IF NOT EXISTS {schema}.ngn_voice_preferences ("
                "id INTEGER PRIMARY KEY CHECK(id = 1), version INTEGER NOT NULL, "
                "payload TEXT NOT NULL, revision TEXT NOT NULL)"
            )
        db.execute(
            "INSERT OR IGNORE INTO voice_global.ngn_voice_preferences VALUES (1, 1, ?, ?)",
            (VoicePreferences().model_dump_json(), secrets.token_hex(32)),
        )
        db.execute(
            "INSERT OR IGNORE INTO main.ngn_voice_preferences VALUES (1, 1, ?, ?)",
            (VoiceOverrides().model_dump_json(exclude_unset=True), secrets.token_hex(32)),
        )

    def transaction(
        self, operation: Callable[[sqlite3.Connection, VoicePreferences, VoiceOverrides, str, str], T]
    ) -> T:
        directory = self.global_db.parent
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            descriptor = os.open(self.global_db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with closing(sqlite3.connect(self.workspace_db, timeout=5)) as db:
            db.execute("ATTACH DATABASE ? AS voice_global", (str(self.global_db),))
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._initialize(db)
                global_values, global_revision = self._row(db, "voice_global")
                local_values, local_revision = self._row(db, "main")
                assert isinstance(global_values, VoicePreferences)
                assert isinstance(local_values, VoiceOverrides)
                return operation(db, global_values, local_values, global_revision, local_revision)
