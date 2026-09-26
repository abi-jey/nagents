"""Revisioned workspace Live connections; private keys never enter process defaults."""

from __future__ import annotations

import asyncio
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from contextlib import closing
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Literal
from typing import TypeVar
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr
from pydantic import field_validator
from pydantic import model_validator

from nagents.live import LiveConfig
from nagents.provider.auth import validate_prefix

from ._async import join_owned

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from collections.abc import Coroutine
    from collections.abc import Iterator
    from pathlib import Path

LIVE_VOICES = ("marin", "cedar")
LIVE_PROVIDERS = ("openai", "openai_compatible", "azure_openai_compatible_v1")
DEFAULT_BASE_URL = "https://api.openai.com/v1"
MAX_SETTINGS_BYTES = 8192
MAX_KEY_BYTES = 4096
T = TypeVar("T")
Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ModelId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")]


class LiveValues(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    enabled: bool
    provider: Literal["openai", "openai_compatible", "azure_openai_compatible_v1"]
    model: ModelId
    backend_model: ModelId
    voice: Literal["marin", "cedar"]
    base_url: str = Field(max_length=2048)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        if value:
            try:
                validate_prefix(value)
            except ValueError:
                raise ValueError(
                    "Use an HTTPS API prefix without credentials, query parameters, or fragments."
                ) from None
        return value

    @classmethod
    def defaults(cls) -> LiveValues:
        return cls(
            enabled=False,
            provider="openai",
            model="gpt-live-1",
            backend_model=LiveConfig().backend_model,
            voice="marin",
            base_url="",
        )

    def connection(self) -> tuple[str, str]:
        """Identity follows the provider's effective API prefix, not display spelling."""
        if not self.base_url and self.provider == "azure_openai_compatible_v1":
            return self.provider, ""
        parsed = urlsplit(self.base_url or DEFAULT_BASE_URL)
        host = (parsed.hostname or "").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            host += f":{port}"
        path = parsed.path.rstrip("/")
        if self.provider == "azure_openai_compatible_v1" and not path.endswith("/v1"):
            path += "/v1"
        return self.provider, urlunsplit((parsed.scheme.lower(), host, path, "", ""))


class LiveSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    revision: Revision
    values: LiveValues
    api_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""), max_length=MAX_KEY_BYTES, exclude=True, repr=False
    )
    clear_api_key: bool = False

    @field_validator("api_key")
    @classmethod
    def key(cls, value: SecretStr) -> SecretStr:
        if any(not 33 <= ord(char) <= 126 for char in value.get_secret_value()):
            raise ValueError("The API key must contain only visible ASCII characters.")
        return value

    @model_validator(mode="after")
    def key_action(self) -> LiveSettingsInput:
        if self.api_key.get_secret_value() and self.clear_api_key:
            raise ValueError("Choose a replacement key or clear the key, not both.")
        return self


@dataclass(frozen=True)
class LiveConnection:
    values: LiveValues
    revision: str
    api_key: SecretStr = field(repr=False)

    @property
    def key_configured(self) -> bool:
        return bool(self.api_key.get_secret_value())

    def snapshot(self) -> dict[str, object]:
        return {
            "values": self.values.model_dump(),
            "revision": self.revision,
            "key_configured": self.key_configured,
            "providers": list(LIVE_PROVIDERS),
            "voices": list(LIVE_VOICES),
        }


def _public_values(values: LiveValues, *keys: SecretStr) -> None:
    strings = (values.provider, values.model, values.backend_model, values.voice, values.base_url)
    for key in keys:
        secret = key.get_secret_value()
        if secret and any(secret in value for value in strings):
            raise ValueError("Keep API keys in the write-only API key field.")


class LiveSettings:
    """Short SQLite transactions are the source of truth, including across processes.

    No cached connection can outlive a failed reload or overwrite a later commit.
    A local reservation excludes saves during admission/provisioning; a ContextVar
    pins the checked connection in the runtime supervisor's inherited task context.
    Reads can continue while a call uses that captured configuration.
    """

    def __init__(self, db_path: Path, *, demo: bool, active: Callable[[], str]) -> None:
        self.db_path = db_path
        self.demo = demo
        self.active = active
        self._busy = False
        self._closed = False
        self._loaded = False
        self._tasks: set[asyncio.Task[object]] = set()
        self._admission: ContextVar[LiveConnection] = ContextVar("live_connection")

    async def _owned(self, operation: Coroutine[object, object, T]) -> T:
        task = asyncio.create_task(operation)
        self._tasks.add(task)
        try:
            return await join_owned(task)
        finally:
            self._tasks.discard(task)

    async def _transaction(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def execute() -> T:
            with closing(sqlite3.connect(self.db_path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                return operation(db)

        return await asyncio.to_thread(execute)

    @staticmethod
    def _read(db: sqlite3.Connection) -> LiveConnection:
        with closing(
            db.execute(
                "SELECT s.version, CASE WHEN typeof(s.values_json) = 'text' AND "
                "length(CAST(s.values_json AS BLOB)) <= ? THEN s.values_json END, "
                "CASE WHEN length(s.revision) = 64 THEN s.revision END, "
                "k.id, CASE WHEN length(k.provider) <= 64 THEN k.provider END, "
                "CASE WHEN length(k.endpoint) <= 2048 THEN k.endpoint END, "
                "CASE WHEN typeof(k.secret) = 'text' AND length(CAST(k.secret AS BLOB)) <= ? THEN k.secret END "
                "FROM ngn_web_live_settings s LEFT JOIN ngn_web_live_key k ON k.id = s.id WHERE s.id = 1",
                (MAX_SETTINGS_BYTES, MAX_KEY_BYTES),
            )
        ) as cursor:
            row = cursor.fetchone()
        if row is None:
            raise ValueError("Missing Live settings")
        version, payload, revision, key_id, provider, endpoint, secret = row
        if (
            type(version) is not int
            or version != 1
            or not isinstance(payload, str)
            or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{64}", revision)
        ):
            raise ValueError("Invalid Live settings")
        values = LiveValues.model_validate_json(payload)
        key = SecretStr("")
        if key_id is not None:
            if (
                (provider, endpoint) != values.connection()
                or not isinstance(secret, str)
                or not secret
                or any(not 33 <= ord(char) <= 126 for char in secret)
            ):
                raise ValueError("Invalid saved Live key")
            key = SecretStr(secret)
        _public_values(values, key)
        return LiveConnection(values, revision, key)

    async def load(self) -> None:
        if self._closed:
            raise HTTPException(503, "Live connection settings are unavailable.")

        async def initialize() -> None:
            def create(db: sqlite3.Connection) -> None:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ngn_web_live_settings ("
                    "id INTEGER PRIMARY KEY CHECK(id = 1), version INTEGER NOT NULL, "
                    "values_json TEXT NOT NULL, revision TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ngn_web_live_key ("
                    "id INTEGER PRIMARY KEY CHECK(id = 1), provider TEXT NOT NULL, "
                    "endpoint TEXT NOT NULL, secret TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT OR IGNORE INTO ngn_web_live_settings VALUES (1, 1, ?, ?)",
                    (LiveValues.defaults().model_dump_json(), secrets.token_hex(32)),
                )
                self._read(db)

            try:
                await self._transaction(create)
            except Exception:
                raise HTTPException(
                    503, "Live connection settings could not be loaded. Check the workspace database."
                ) from None
            self._loaded = True

        await self._owned(initialize())

    def _ready(self) -> None:
        if self._closed or not self._loaded:
            raise HTTPException(503, "Live connection settings are unavailable.")

    async def connection(self) -> LiveConnection:
        self._ready()

        async def read() -> LiveConnection:
            try:
                return await self._transaction(self._read)
            except Exception:
                raise HTTPException(
                    503, "Live connection settings could not be loaded. Open Connection settings and retry."
                ) from None

        return await self._owned(read())

    async def snapshot(self) -> dict[str, object]:
        return (await self.connection()).snapshot()

    @contextmanager
    def _idle(self) -> Iterator[None]:
        self._ready()
        if self._busy or self.active():
            raise HTTPException(409, "Live is busy. End the call or wait for Connection settings to finish saving.")
        self._busy = True
        try:
            yield
        finally:
            self._busy = False

    async def change(self, body: LiveSettingsInput) -> dict[str, object]:
        def save(db: sqlite3.Connection) -> LiveConnection:
            current = self._read(db)
            if current.revision != body.revision:
                raise HTTPException(409, "Live settings changed. Reload Connection settings before saving.")
            try:
                _public_values(body.values, current.api_key, body.api_key)
            except ValueError:
                raise HTTPException(422, "Keep API keys in the write-only API key field.") from None
            key = body.api_key
            if (
                not key.get_secret_value()
                and not body.clear_api_key
                and body.values.connection() == current.values.connection()
            ):
                key = current.api_key
            revision = secrets.token_hex(32)
            payload = body.values.model_dump_json()
            if len(payload.encode("utf-8")) > MAX_SETTINGS_BYTES:
                raise HTTPException(422, "Live connection settings exceed the storage limit.")
            db.execute(
                "UPDATE ngn_web_live_settings SET values_json = ?, revision = ? WHERE id = 1",
                (payload, revision),
            )
            db.execute("DELETE FROM ngn_web_live_key WHERE id = 1")
            if key.get_secret_value():
                db.execute(
                    "INSERT INTO ngn_web_live_key VALUES (1, ?, ?, ?)",
                    (*body.values.connection(), key.get_secret_value()),
                )
            return LiveConnection(body.values, revision, key)

        async def commit() -> dict[str, object]:
            try:
                connection = await self._transaction(save)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(
                    500, "Live settings could not be saved. Reload Connection settings before trying again."
                ) from None
            return connection.snapshot()

        with self._idle():
            return await self._owned(commit())

    @asynccontextmanager
    async def admit(self, revision: str) -> AsyncIterator[LiveConnection]:
        with self._idle():
            connection = await self.connection()
            self._ready()  # Shutdown may have started while the database read was running.
            if connection.revision != revision:
                raise HTTPException(409, "Live settings changed. Reload Connection settings before connecting.")
            token = self._admission.set(connection)
            try:
                yield connection
            finally:
                self._admission.reset(token)

    def admitted(self) -> LiveConnection:
        """Only the supervisor created inside admit() may acquire this captured key."""
        try:
            return self._admission.get()
        except LookupError:
            raise HTTPException(503, "Live connection has not been admitted.") from None

    async def shutdown(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks)

        async def finish() -> None:
            # Request owners report their own errors; join every database worker
            # before propagating cancellation of this shutdown owner.
            await asyncio.gather(*tasks, return_exceptions=True)

        await join_owned(asyncio.create_task(finish()))
