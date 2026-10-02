"""Revisioned workspace Live connections; private keys never enter process defaults."""

from __future__ import annotations

import asyncio
import hashlib
import os
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

from nagents.harness.auth import OpenAIAuth
from nagents.harness.connection import live_auth_available
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.live import LiveConfig
from nagents.provider.auth import validate_prefix

from ._async import join_owned
from .live_auth import LOGIN_VOICES
from .live_auth import resolve_voice_auth
from .voice_preferences import Scope
from .voice_preferences import VoiceOverrides
from .voice_preferences import VoicePreferenceStore
from .voice_preferences import VoicePreferences

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from collections.abc import Coroutine
    from collections.abc import Iterator
    from pathlib import Path

    from .live_auth import LoginCredentials
    from .live_auth import VoiceAuth

LIVE_VOICES = ("marin", "cedar")
LIVE_PROVIDERS = ("openai", "openai_compatible", "azure_openai_compatible_v1")
NAMED_LIVE_PROVIDERS = (*LIVE_PROVIDERS, "foundry")
DEFAULT_BASE_URL = "https://api.openai.com/v1"
MAX_SETTINGS_BYTES = 8192
MAX_KEY_BYTES = 4096
T = TypeVar("T")
Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ModelId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")]


class LiveValues(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    enabled: bool
    connection_id: str = Field(default="", max_length=64, pattern=r"^$|^[a-z][a-z0-9_-]{0,63}$")
    backend_mode: Literal["assistant", "hosted"] = "assistant"
    provider: Literal["openai", "openai_compatible", "azure_openai_compatible_v1", "foundry"]
    model: ModelId
    backend_model: ModelId
    voice: Literal["marin", "cedar", "arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale"]
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
            backend_mode="assistant",
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


class GlobalVoiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    scope: Literal["global"]
    revision: Revision
    preferences: VoicePreferences


class WorkspaceVoiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    scope: Literal["workspace"]
    revision: Revision
    overrides: VoiceOverrides


@dataclass(frozen=True)
class LiveConnection:
    values: LiveValues
    revision: str
    api_key: SecretStr = field(repr=False)
    profile: ProviderProfile | None = field(default=None, repr=False)
    profile_name: str = ""
    credential_available: bool = False
    connection_scope: str = ""
    voice_scope: str = ""
    global_preferences: VoicePreferences | None = None
    overrides: VoiceOverrides | None = None
    connections: tuple[tuple[str, str, str], ...] = ()
    voice_auth: VoiceAuth = "api-key"
    login_credentials: LoginCredentials | None = field(default=None, repr=False)

    @property
    def voices(self) -> tuple[str, ...]:
        return LOGIN_VOICES if self.voice_auth == "chatgpt" else LIVE_VOICES

    @property
    def voice_model(self) -> str:
        return (
            "gpt-live-1-codex"
            if self.voice_auth == "chatgpt" and self.values.model == "gpt-live-1"
            else self.values.model
        )

    @property
    def key_configured(self) -> bool:
        return bool(self.api_key.get_secret_value()) or self.credential_available

    def snapshot(self) -> dict[str, object]:
        result: dict[str, object] = {
            "values": self.values.model_dump(),
            "revision": self.revision,
            "key_configured": self.key_configured,
            "providers": list(LIVE_PROVIDERS),
            "voices": list(self.voices),
            "voice_auth": self.voice_auth,
        }
        if self.voice_scope and self.global_preferences is not None and self.overrides is not None:
            selected = self.overrides.selected() if self.voice_scope == "workspace" else {}
            result.update(
                source="providers",
                scope=self.voice_scope,
                global_preferences=self.global_preferences.model_dump(),
                overrides=selected,
                origins={name: "workspace" if name in selected else "global" for name in VoicePreferences.model_fields},
                profile_name=self.profile_name,
                live_supported=self.profile is not None and self.profile.kind in NAMED_LIVE_PROVIDERS,
                connections=[
                    {"name": name, "provider": provider, "scope": scope} for name, provider, scope in self.connections
                ],
            )
        if self.profile is not None:
            result.update(
                profile_name=self.profile_name,
                api_key_env=self.profile.key_env,
                auth=self.profile.auth,
                live_supported=self.profile.kind in NAMED_LIVE_PROVIDERS,
                connection_scope=self.connection_scope,
            )
        return result


def _public_values(values: LiveValues, *keys: SecretStr) -> None:
    strings = (values.provider, values.model, values.backend_model, values.voice, values.base_url, values.connection_id)
    for key in keys:
        secret = key.get_secret_value()
        if secret and any(secret in value for value in strings):
            raise ValueError("Keep API keys in the write-only API key field.")


def _voice_revision(
    scope: Scope,
    global_revision: str,
    global_provider_revision: str,
    local_revision: str,
    local_provider_revision: str,
    active: str,
) -> str:
    parts = [scope, global_revision, global_provider_revision]
    if scope == "workspace":
        parts.extend((local_revision, local_provider_revision, active))
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


class LiveSettings:
    """Short SQLite transactions are the source of truth, including across processes.

    No cached connection can outlive a failed reload or overwrite a later commit.
    A local reservation excludes saves during admission/provisioning; a ContextVar
    pins the checked connection in the runtime supervisor's inherited task context.
    Reads can continue while a call uses that captured configuration.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        demo: bool,
        active: Callable[[], str],
        providers: ScopedProviderRegistryStore | None = None,
        auth: OpenAIAuth | None = None,
    ) -> None:
        self.db_path = db_path
        self.demo = demo
        self.active = active
        self._busy = False
        self._closed = False
        self._loaded = False
        self._tasks: set[asyncio.Task[object]] = set()
        self._admission: ContextVar[LiveConnection] = ContextVar("live_connection")
        self.providers = providers or ScopedProviderRegistryStore(db_path.parent)
        self.voice = VoicePreferenceStore(db_path, self.providers)
        self.auth = auth or OpenAIAuth()
        self._session: ContextVar[str] = ContextVar("live_chat_session")

    def _scoped(self, scope: Scope) -> LiveConnection:
        global_registry = self.providers.load_scope("global")
        local = self.providers.load_scope("workspace")
        registry = global_registry if scope == "global" else self.providers.load()
        name = registry.active

        def read(
            db: sqlite3.Connection,
            global_values: VoicePreferences,
            overrides: VoiceOverrides,
            global_revision: str,
            local_revision: str,
        ) -> LiveConnection:
            del db
            revision = _voice_revision(
                scope,
                global_revision,
                global_registry.revision,
                local_revision,
                local.revision,
                name,
            )
            preferences = VoicePreferences.model_validate(
                {
                    **global_values.model_dump(),
                    **(overrides.selected() if scope == "workspace" else {}),
                }
            )
            selected_name = preferences.connection_id or name
            profile = registry.providers.get(selected_name)
            connection_scope = "workspace" if scope == "workspace" and selected_name in local.providers else "global"
            voice_auth, credentials = (
                resolve_voice_auth(profile, self.auth, preferences.model) if profile else ("api-key", None)
            )
            voices = LOGIN_VOICES if voice_auth == "chatgpt" else LIVE_VOICES
            values = LiveValues.model_validate(
                {
                    **preferences.model_dump(),
                    "provider": profile.kind
                    if profile is not None and profile.kind in NAMED_LIVE_PROVIDERS
                    else "openai",
                    "base_url": profile.base_url if profile is not None else "",
                    "model": ("gpt-live-1-codex" if voice_auth == "chatgpt" else "gpt-live-1")
                    if preferences.model in {"gpt-live-1", "gpt-live-1-codex"}
                    else preferences.model,
                    "voice": preferences.voice
                    if preferences.voice in voices
                    else ("cove" if voice_auth == "chatgpt" else "marin"),
                }
            )
            supported = profile is not None and profile.kind in NAMED_LIVE_PROVIDERS
            return LiveConnection(
                values,
                revision,
                SecretStr(
                    os.environ.get(profile.key_env, "")
                    if supported and profile is not None and voice_auth != "chatgpt"
                    else ""
                ),
                profile=profile,
                profile_name=selected_name,
                credential_available=(
                    credentials is not None if voice_auth == "chatgpt" else live_auth_available(profile, values.model)
                )
                if supported and profile is not None
                else False,
                connection_scope=connection_scope,
                voice_scope=scope,
                global_preferences=global_values,
                overrides=overrides,
                voice_auth=voice_auth,
                login_credentials=credentials,
                connections=tuple(
                    (
                        identifier,
                        candidate.kind,
                        "workspace" if scope == "workspace" and identifier in local.providers else "global",
                    )
                    for identifier, candidate in registry.providers.items()
                    if candidate.kind in NAMED_LIVE_PROVIDERS
                ),
            )

        connection = self.voice.transaction(read)
        if self.providers.load_scope("global").revision != global_registry.revision or (
            scope == "workspace" and self.providers.load_scope("workspace").revision != local.revision
        ):
            raise HTTPException(409, "Provider connection changed. Reload Voice settings before connecting.")
        return connection

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
        # Saved connections created before backend_mode existed were hosted.
        # Keep their behavior until the user explicitly chooses another mode.
        if "backend_mode" not in values.model_fields_set:
            values = values.model_copy(update={"backend_mode": "hosted"})
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
                if not self.providers.load().active:
                    self._read(db)

            try:
                await self._transaction(create)
                await asyncio.to_thread(self._scoped, "workspace")
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
        scoped = await self.scoped("workspace")
        if self.providers.load().active or scoped.values.connection_id:
            return scoped

        async def read() -> LiveConnection:
            try:
                return await self._transaction(self._read)
            except Exception:
                raise HTTPException(
                    503, "Live connection settings could not be loaded. Open Connection settings and retry."
                ) from None

        return await self._owned(read())

    async def scoped(self, scope: Scope) -> LiveConnection:
        self._ready()

        async def read() -> LiveConnection:
            try:
                return await asyncio.to_thread(self._scoped, scope)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(
                    503, "Voice preferences could not be loaded. Check the settings database."
                ) from None

        return await self._owned(read())

    async def snapshot(self, scope: Scope | None = None) -> dict[str, object]:
        return (await (self.scoped(scope) if scope is not None else self.connection())).snapshot()

    @contextmanager
    def _idle(self) -> Iterator[None]:
        self._ready()
        if self._busy or self.active():
            raise HTTPException(409, "Live is busy. End the call or wait for Voice settings to finish saving.")
        self._busy = True
        try:
            yield
        finally:
            self._busy = False

    async def change(self, body: LiveSettingsInput | GlobalVoiceInput | WorkspaceVoiceInput) -> dict[str, object]:
        if isinstance(body, (GlobalVoiceInput, WorkspaceVoiceInput)):
            with self._idle():
                scope = body.scope
                global_registry = self.providers.load_scope("global")
                local_registry = self.providers.load_scope("workspace")
                name = global_registry.active if scope == "global" else self.providers.load().active

                def save_voice(
                    db: sqlite3.Connection,
                    global_values: VoicePreferences,
                    overrides: VoiceOverrides,
                    global_revision: str,
                    local_revision: str,
                ) -> None:
                    revision = _voice_revision(
                        scope,
                        global_revision,
                        global_registry.revision,
                        local_revision,
                        local_registry.revision,
                        name,
                    )
                    if revision != body.revision:
                        raise HTTPException(409, "Voice preferences changed. Reload settings before saving.")
                    if self.providers.load_scope("global").revision != global_registry.revision or (
                        scope == "workspace"
                        and self.providers.load_scope("workspace").revision != local_registry.revision
                    ):
                        raise HTTPException(409, "Provider connection changed. Reload Voice settings before saving.")
                    preferences = body.preferences if isinstance(body, GlobalVoiceInput) else body.overrides
                    next_global = (
                        preferences
                        if isinstance(preferences, VoicePreferences) and scope == "global"
                        else global_values
                    )
                    next_local = preferences if scope == "workspace" else overrides
                    assert isinstance(next_global, VoicePreferences)
                    assert isinstance(next_local, VoiceOverrides)
                    selected_registry = self.providers.load()
                    requested = body.preferences if isinstance(body, GlobalVoiceInput) else body.overrides
                    selected_id = requested.connection_id
                    available = global_registry if scope == "global" else selected_registry
                    if selected_id:
                        selected_profile = available.providers.get(selected_id)
                        if selected_profile is None or selected_profile.kind not in NAMED_LIVE_PROVIDERS:
                            raise HTTPException(422, "Choose an existing GPT-Live provider connection in this scope.")
                    keys = tuple(
                        SecretStr(os.environ.get(profile.key_env, ""))
                        for profile in (*global_registry.providers.values(), *selected_registry.providers.values())
                    )
                    try:
                        for selected in (
                            next_global.model_dump(),
                            {
                                **next_global.model_dump(),
                                **next_local.selected(),
                            },
                        ):
                            _public_values(
                                LiveValues.model_validate({**selected, "provider": "openai", "base_url": ""}), *keys
                            )
                    except ValueError:
                        raise HTTPException(422, "Keep API keys out of Voice preferences.") from None
                    payload = preferences.model_dump_json(exclude_unset=scope == "workspace")
                    if len(payload.encode("utf-8")) > MAX_SETTINGS_BYTES:
                        raise HTTPException(422, "Voice preferences exceed the storage limit.")
                    schema = "voice_global" if scope == "global" else "main"
                    db.execute(
                        f"UPDATE {schema}.ngn_voice_preferences SET payload = ?, revision = ? WHERE id = 1",
                        (payload, secrets.token_hex(32)),
                    )

                async def commit_voice() -> dict[str, object]:
                    try:
                        await asyncio.to_thread(self.voice.transaction, save_voice)
                    except HTTPException:
                        raise
                    except Exception:
                        raise HTTPException(500, "Voice preferences could not be saved. Reload settings.") from None
                    return await self.snapshot(scope)

                return await self._owned(commit_voice())

        if self.providers.load().active:
            raise HTTPException(422, "Named provider connections use scoped Voice preferences.")
        if body.values.voice not in LIVE_VOICES:
            raise HTTPException(422, "Choose a supported API-key Live voice.")

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
    async def admit(self, revision: str, session_id: str = "") -> AsyncIterator[LiveConnection]:
        with self._idle():
            connection = await self.connection()
            self._ready()  # Shutdown may have started while the database read was running.
            if connection.revision != revision:
                raise HTTPException(409, "Live settings changed. Reload Voice settings before connecting.")
            token = self._admission.set(connection)
            session_token = self._session.set(session_id)
            try:
                yield connection
            finally:
                self._session.reset(session_token)
                self._admission.reset(token)

    def admitted(self) -> LiveConnection:
        """Only the supervisor created inside admit() may acquire this captured key."""
        try:
            return self._admission.get()
        except LookupError:
            raise HTTPException(503, "Live connection has not been admitted.") from None

    def admitted_session(self) -> str:
        """Return the verified root captured with this call's connection."""
        try:
            return self._session.get()
        except LookupError:
            raise HTTPException(503, "Live conversation has not been admitted.") from None

    async def shutdown(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks)

        async def finish() -> None:
            # Request owners report their own errors; join every database worker
            # before propagating cancellation of this shutdown owner.
            await asyncio.gather(*tasks, return_exceptions=True)

        await join_owned(asyncio.create_task(finish()))
