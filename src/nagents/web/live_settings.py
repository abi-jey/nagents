"""Revisioned workspace Live connections; private keys never enter process defaults."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from contextlib import asynccontextmanager
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Literal
from typing import TypeVar

from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr
from pydantic import field_validator

from nagents.harness.auth import OpenAIAuth
from nagents.harness.connection import codex_discovery_selected
from nagents.harness.connection import live_auth_available
from nagents.harness.live_profiles import LIVE_FIELDS
from nagents.harness.live_profiles import LiveProfile
from nagents.harness.providers import KINDS
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.live import LiveConfig
from nagents.provider.auth import validate_prefix

from ._async import join_owned
from .live_auth import LOGIN_VOICES
from .live_auth import resolve_voice_auth
from .voice_preferences import MAX_VOICE_BYTES
from .voice_preferences import ContextMode
from .voice_preferences import Scope
from .voice_preferences import VoiceInstructions
from .voice_preferences import VoiceOverrides
from .voice_preferences import VoicePreferenceStore
from .voice_preferences import VoicePreferences

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from collections.abc import Coroutine
    from collections.abc import Iterator
    from pathlib import Path

    from nagents.harness import Harness

    from .live_auth import LoginCredentials
    from .live_auth import VoiceAuth

LIVE_VOICES = ("marin", "cedar")
LIVE_PROVIDERS = ("openai", "openai_compatible", "azure_openai_compatible_v1")
NAMED_LIVE_PROVIDERS = tuple(name for name, kind in KINDS.items() if kind.live)
MAX_SETTINGS_BYTES = MAX_VOICE_BYTES
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
    instructions: VoiceInstructions = ""
    context_mode: ContextMode = "recent"
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


class AgentVoiceRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    scope: Literal["agent"]
    agent: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    revision: Revision


class AgentVoiceInput(AgentVoiceRevision):
    overrides: dict[str, object]

    @field_validator("overrides")
    @classmethod
    def profile(cls, value: dict[str, object]) -> dict[str, object]:
        return LiveProfile.parse(value).selected()


@dataclass(frozen=True)
class AgentLiveLayer:
    agent: str = ""
    definition: LiveProfile = field(default_factory=LiveProfile)
    overrides: LiveProfile = field(default_factory=LiveProfile)
    preferences_revision: str = ""
    definitions_revision: str = ""
    source: str = ""
    path: str = ""
    present: bool = False


_EMPTY_AGENT_LAYER = AgentLiveLayer()


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
    agent_settings: dict[str, object] = field(default_factory=dict)

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
        result.update(self.agent_settings)
        return result


def _public_values(values: LiveValues, *keys: SecretStr) -> None:
    strings = (
        values.provider,
        values.model,
        values.backend_model,
        values.voice,
        values.base_url,
        values.connection_id,
        values.instructions,
    )
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
        harness: Harness | None = None,
        selected_agent: Callable[[], str] = lambda: "",
    ) -> None:
        self.harness = harness
        self.selected_agent = selected_agent
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

    def _agent_layer(self, agent: str) -> AgentLiveLayer:
        if not agent or self.harness is None:
            if agent:
                raise HTTPException(404, "Agent Live settings are unavailable.")
            return AgentLiveLayer()
        self.harness.refresh_agent_definitions()
        if agent not in self.harness.config.profile_names:
            raise HTTPException(404, "This agent profile is no longer available.")
        profile = self.harness.config.profile(agent)
        try:
            saved = self.providers.agent_models.load(agent)
            overrides = LiveProfile.parse(saved.preferences.get("live", {}))
        except (OSError, ValueError):
            raise HTTPException(422, "Agent Live preferences could not be read.") from None
        return AgentLiveLayer(
            agent,
            profile.live,
            overrides,
            saved.revision,
            self.harness.profile_revision,
            profile.source,
            str(saved.path),
            "live" in saved.preferences,
        )

    def _scoped(self, scope: Scope, agent: AgentLiveLayer = _EMPTY_AGENT_LAYER) -> LiveConnection:
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
            inherited_values = {
                **{
                    key: value
                    for key, value in preferences.model_dump().items()
                    if key not in {"connection_id", "model"}
                },
                "model": {"provider": preferences.connection_id or name, "id": preferences.model},
            }
            parent_profile = registry.providers.get(preferences.connection_id or name)
            parent_auth, _ = (
                resolve_voice_auth(parent_profile, self.auth, preferences.model)
                if parent_profile
                else ("api-key", None)
            )
            parent_voices = LOGIN_VOICES if parent_auth == "chatgpt" else LIVE_VOICES
            if preferences.voice not in parent_voices:
                inherited_values["voice"] = "sol" if parent_auth == "chatgpt" else "marin"
            if preferences.model in {"gpt-live-1", "gpt-live-1-codex"}:
                inherited_values["model"] = {
                    "provider": preferences.connection_id or name,
                    "id": "gpt-live-1-codex" if parent_auth == "chatgpt" else "gpt-live-1",
                }
            definition = agent.definition.selected()
            overlay = agent.overrides.selected()
            agent_fields = overlay if agent.present else definition
            if agent.agent:
                revision = hashlib.sha256(
                    f"{revision}:{agent.agent}:{agent.preferences_revision}:{agent.definitions_revision}".encode()
                ).hexdigest()
                flattened = {key: value for key, value in agent_fields.items() if key != "model"}
                if "model" in agent_fields:
                    binding = agent_fields["model"]
                    assert isinstance(binding, dict)
                    flattened.update(connection_id=binding["provider"], model=binding["id"])
                preferences = VoicePreferences.model_validate({**preferences.model_dump(), **flattened})
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
                    else ("sol" if voice_auth == "chatgpt" else "marin"),
                }
            )
            supported = profile is not None and profile.kind in NAMED_LIVE_PROVIDERS
            local_fields = overrides.selected() if scope == "workspace" else {}
            origins = {
                key: "agent"
                if key in agent_fields
                else "workspace"
                if key in local_fields
                else "global"
                if key in global_values.model_fields_set
                else "builtin"
                for key in sorted(LIVE_FIELDS - {"model"})
            }
            origins["model"] = (
                "agent"
                if "model" in agent_fields
                else "workspace"
                if {"connection_id", "model"} & local_fields.keys()
                else "global"
                if {"connection_id", "model"} & global_values.model_fields_set
                else "builtin"
            )
            details: dict[str, object] = {
                "effective": {
                    **{
                        key: value
                        for key, value in values.model_dump().items()
                        if key in {"enabled", "voice", "instructions", "context_mode", "backend_mode", "backend_model"}
                    },
                    "model": {"provider": selected_name, "id": values.model},
                },
                "origins": origins if agent.agent else {**origins, "connection_id": origins["model"]},
            }
            if agent.agent:
                details.update(
                    scope="agent",
                    agent=agent.agent,
                    overrides=overlay,
                    saved_preferences=overlay,
                    definition_live=definition,
                    saved_live_present=agent.present,
                    inherited=inherited_values,
                    agent_overrides=agent_fields,
                    preferences_revision=agent.preferences_revision,
                    definitions_revision=agent.definitions_revision,
                    source_path=agent.source,
                    preference_path=agent.path,
                    field_sources={
                        key: "overlay"
                        if key in overlay
                        else "definition"
                        if not agent.present and key in definition
                        else origins[key]
                        for key in origins
                    },
                )
            return LiveConnection(
                values,
                revision,
                SecretStr(
                    os.environ.get(profile.key_env, "")
                    if supported
                    and profile is not None
                    and voice_auth != "chatgpt"
                    and not codex_discovery_selected(profile)
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
                agent_settings=details,
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

    async def load(self) -> None:
        if self._closed:
            raise HTTPException(503, "Live connection settings are unavailable.")

        async def initialize() -> None:
            try:
                await asyncio.to_thread(self._scoped, "workspace")
            except Exception:
                raise HTTPException(
                    503, "Voice preferences could not be loaded. Check the workspace database."
                ) from None
            self._loaded = True

        await self._owned(initialize())

    def _ready(self) -> None:
        if self._closed or not self._loaded:
            raise HTTPException(503, "Live connection settings are unavailable.")

    async def connection(self) -> LiveConnection:
        return await self.scoped("workspace", agent=self.selected_agent())

    async def scoped(self, scope: Scope, *, agent: str = "") -> LiveConnection:
        self._ready()
        layer = self._agent_layer(agent)

        async def read() -> LiveConnection:
            try:
                return await asyncio.to_thread(self._scoped, scope, layer)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(
                    503, "Voice preferences could not be loaded. Check the settings database."
                ) from None

        return await self._owned(read())

    async def snapshot(self, scope: Scope | Literal["agent"], agent: str = "") -> dict[str, object]:
        if scope == "agent":
            if not agent:
                raise HTTPException(422, "Choose an agent for Live settings.")
            return (await self.scoped("workspace", agent=agent)).snapshot()
        return (await self.scoped(scope)).snapshot()

    async def change_agent(self, body: AgentVoiceInput | AgentVoiceRevision) -> dict[str, object]:
        selected = body.overrides if isinstance(body, AgentVoiceInput) else {}
        with self._idle(allow_active=True):
            current = await self.scoped("workspace", agent=body.agent)
            if current.revision != body.revision:
                raise HTTPException(409, "Agent Live preferences changed. Refresh before saving.")
            model = selected.get("model")
            if isinstance(model, dict):
                profile = self.providers.load().providers.get(str(model["provider"]))
                if profile is None or profile.kind not in NAMED_LIVE_PROVIDERS:
                    raise HTTPException(422, "Choose an existing GPT-Live provider connection.")
            registry = self.providers.load()
            keys = [os.environ.get(profile.key_env, "") for profile in registry.providers.values()]
            strings = tuple(value for value in selected.values() if isinstance(value, str))
            if isinstance(model, dict):
                strings += tuple(value for value in model.values() if isinstance(value, str))
            if any(key and any(key in value for value in strings) for key in keys):
                raise HTTPException(422, "Keep API keys out of Voice preferences.")
            layer = self._agent_layer(body.agent)
            if layer.preferences_revision != current.agent_settings["preferences_revision"] or (
                layer.definitions_revision != current.agent_settings["definitions_revision"]
            ):
                raise HTTPException(409, "Agent Live preferences changed. Refresh before saving.")
            try:
                if isinstance(body, AgentVoiceInput):
                    self.providers.agent_models.patch(
                        body.agent, {"live": selected}, expected=layer.preferences_revision
                    )
                else:
                    self.providers.agent_models.remove(body.agent, ("live",), expected=layer.preferences_revision)
            except ValueError as error:
                raise HTTPException(409 if "changed" in str(error) else 422, str(error)) from None
            except OSError:
                raise HTTPException(500, "Agent Live preferences could not be saved.") from None
            return await self.snapshot("agent", body.agent)

    @contextmanager
    def _idle(self, *, allow_active: bool = False) -> Iterator[None]:
        self._ready()
        if self._busy or (self.active() and not allow_active):
            raise HTTPException(409, "Live is busy. End the call or wait for Voice settings to finish saving.")
        self._busy = True
        try:
            yield
        finally:
            self._busy = False

    async def change(self, body: GlobalVoiceInput | WorkspaceVoiceInput) -> dict[str, object]:
        with self._idle(allow_active=True):
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
                    scope == "workspace" and self.providers.load_scope("workspace").revision != local_registry.revision
                ):
                    raise HTTPException(409, "Provider connection changed. Reload Voice settings before saving.")
                preferences = body.preferences if isinstance(body, GlobalVoiceInput) else body.overrides
                next_global = (
                    preferences if isinstance(preferences, VoicePreferences) and scope == "global" else global_values
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

    @asynccontextmanager
    async def admit(self, revision: str, session_id: str = "") -> AsyncIterator[LiveConnection]:
        with self._idle():
            connection = await self.connection()
            self._ready()  # Shutdown may have started while the database read was running.
            layer = self._agent_layer(self.selected_agent())
            if layer.agent and (
                connection.agent_settings.get("agent") != layer.agent
                or connection.agent_settings.get("preferences_revision") != layer.preferences_revision
                or connection.agent_settings.get("definitions_revision") != layer.definitions_revision
            ):
                raise HTTPException(409, "Selected agent Live settings changed. Refresh before connecting.")
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
