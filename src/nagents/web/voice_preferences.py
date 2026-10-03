"""Global voice defaults and per-workspace field overrides, independent of providers."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Literal
from typing import TypeVar

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from nagents.live import LiveConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from nagents.harness.providers import ScopedProviderRegistryStore

MAX_VOICE_INSTRUCTION_CHARACTERS = 6000
MAX_VOICE_INSTRUCTION_BYTES = 12000
# JSON can escape each control character to six bytes. Leave room for those
# escapes and the remaining preferences while staying below the HTTP body cap.
MAX_VOICE_BYTES = 40 * 1024
ModelId = str
Scope = Literal["global", "workspace"]
ContextMode = Literal["recent", "summary", "none"]
T = TypeVar("T")


def _instructions(value: str) -> str:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("Voice instructions must contain valid Unicode text.") from None
    if size > MAX_VOICE_INSTRUCTION_BYTES:
        raise ValueError("Voice instructions exceed the 12,000-byte UTF-8 size limit.")
    return value


VoiceInstructions = Annotated[str, Field(max_length=MAX_VOICE_INSTRUCTION_CHARACTERS), AfterValidator(_instructions)]


class VoicePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    enabled: bool = False
    connection_id: str = Field(default="", max_length=64, pattern=r"^$|^[a-z][a-z0-9_-]{0,63}$")
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
    voice: Literal[
        "marin", "cedar", "arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale"
    ] = "sol"
    instructions: VoiceInstructions = ""
    context_mode: ContextMode = "recent"


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
