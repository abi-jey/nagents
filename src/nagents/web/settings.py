"""Global defaults and workspace overrides for the shared Harness.

Trusted startup configuration remains the source of defaults and the ceiling.
Provider connections own routing and authentication. Saved credential rows from
older installations are read only for migration and are never exposed by an
endpoint. New settings requests cannot write keys or change connection fields.
"""

import asyncio
import copy
import hashlib
import json
import os
import re
import secrets
from typing import TYPE_CHECKING
from typing import Literal

import aiosqlite
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from nagents.compaction import ModelCompaction
from nagents.harness.agent_documents import agent_document_store
from nagents.harness.model_bindings import ModelBinding
from nagents.harness.providers import API_NAMES
from nagents.harness.providers import PROVIDERS
from nagents.harness.reasoning import resolve_effort
from nagents.provider.reasoning import reasoning_effort_options
from nagents.types import ReasoningEffort

from ._async import join_owned as _join
from .model_capabilities import CatalogCapabilities
from .model_capabilities import catalog_route

if TYPE_CHECKING:
    from typing import Self

    from nagents.harness import Harness
    from nagents.harness.config import HarnessConfig
    from nagents.harness.model_catalog import ModelCatalog

MAX_SETTINGS_BYTES = 16384
_INHERIT_MODEL = ModelBinding()
MAX_API_KEY_BYTES = 4096
PROVIDER_APIS: tuple[str, ...] = API_NAMES
PROVIDER_AUTHS: tuple[str, ...] = ("auto", "api-key", "chatgpt", "codex", "entra")
_CONNECTION_FIELDS = frozenset({"provider", "base_url", "api", "auth", "api_key_env"})
COMPACTION_TRIGGERS: tuple[str, ...] = ("auto", "tokens", "messages", "off")
DEFAULT_COMPACT_TOKENS = 200_000
DEFAULT_COMPACT_MESSAGES = 100
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_GLOBAL_SETTINGS_TABLE = (
    "CREATE TABLE IF NOT EXISTS ngn_web_global_settings (id INTEGER PRIMARY KEY CHECK(id = 1), "
    "version INTEGER NOT NULL, values_json TEXT NOT NULL, revision TEXT NOT NULL)"
)


def _current_compaction(harness: "Harness") -> tuple[str, int, int]:
    """Project the live agent's compaction criteria into settings fields."""
    configured = harness.agent.compaction_strategy
    if configured is None:
        return "off", DEFAULT_COMPACT_TOKENS, DEFAULT_COMPACT_MESSAGES
    if isinstance(configured, ModelCompaction):
        if configured.message_threshold is not None:
            return "messages", DEFAULT_COMPACT_TOKENS, configured.message_threshold
        if configured.token_threshold is not None:
            return (
                "tokens",
                configured.token_budget or (configured.token_threshold * 10 + 6) // 7,
                DEFAULT_COMPACT_MESSAGES,
            )
    return "auto", DEFAULT_COMPACT_TOKENS, DEFAULT_COMPACT_MESSAGES


def _apply_compaction(harness: "Harness", trigger: str, tokens: int, messages: int) -> None:
    """Apply a validated criteria choice to the shared live agent."""
    if trigger == "off":
        harness.agent.compaction_strategy = None
        return
    if trigger == "tokens":
        # Saved preferences express a context window, with 30% kept in reserve.
        harness.agent.compaction_strategy = ModelCompaction(token_budget=tokens)
    elif trigger == "messages":
        harness.agent.compaction_strategy = ModelCompaction(message_threshold=messages)
    else:
        harness.agent.compaction_strategy = ModelCompaction()


class _SettingsValuesV1(BaseModel):
    """The original persisted schema, kept strict for deliberate v1 migration."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)

    model: str = Field(min_length=1, max_length=200)
    agent: str = Field(min_length=1)
    shell_timeout: float = Field(gt=0, le=600)
    max_output: int = Field(ge=1024, le=1048576)
    max_file_bytes: int = Field(ge=1024, le=4194304)
    max_tool_rounds: int = Field(ge=1, le=1000)
    max_subagent_depth: int = Field(ge=0, le=8)

    @field_validator("model", mode="before")
    @classmethod
    def model_id(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.isprintable():
                raise ValueError("Model must not contain control characters")
            return value.strip()
        return value


class SettingsValues(_SettingsValuesV1):
    """Current persisted schema, including allowlisted provider overrides."""

    provider: str = Field(min_length=1, max_length=40)
    base_url: str = Field(max_length=300)
    api: str = Field(min_length=1, max_length=20)
    auth: str = Field(min_length=1, max_length=20)
    api_key_env: str = Field(min_length=1, max_length=64)
    compact_trigger: str = Field(default="auto", min_length=1, max_length=20)
    compact_tokens: int = Field(default=DEFAULT_COMPACT_TOKENS, ge=1024, le=10_000_000)
    compact_messages: int = Field(default=DEFAULT_COMPACT_MESSAGES, ge=1, le=10_000)
    submit_mode: Literal["queue", "interrupt"] = "queue"
    read_only: bool = False
    reasoning_effort: ReasoningEffort = ""

    @field_validator("compact_trigger")
    @classmethod
    def compaction_trigger(cls, value: object) -> object:
        if isinstance(value, str) and value not in COMPACTION_TRIGGERS:
            raise ValueError("Unsupported compaction trigger")
        return value

    @field_validator("provider")
    @classmethod
    def provider_name(cls, value: object) -> object:
        if isinstance(value, str) and value not in PROVIDERS:
            raise ValueError("Unknown provider")
        return value

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.isprintable():
                raise ValueError("Endpoint must be printable text")
            return value.strip()
        return value

    @field_validator("api")
    @classmethod
    def api_name(cls, value: object) -> object:
        if isinstance(value, str) and value not in PROVIDER_APIS:
            raise ValueError("Unsupported API mode")
        return value

    @field_validator("auth")
    @classmethod
    def auth_mode(cls, value: object) -> object:
        if isinstance(value, str) and value not in PROVIDER_AUTHS:
            raise ValueError("Unsupported authentication mode")
        return value

    @field_validator("api_key_env")
    @classmethod
    def api_key_env_name(cls, value: object) -> object:
        if isinstance(value, str) and not _ENV_NAME.fullmatch(value):
            raise ValueError("API key environment name must be a variable name")
        return value

    @classmethod
    def current(cls, harness: "Harness") -> "SettingsValues":
        config = harness.config
        trigger, tokens, messages = _current_compaction(harness)
        try:
            model = harness.default_model_binding().binding.id or harness._selected_model
        except ValueError as error:
            if not config.unavailable_model_binding.configured and "unavailable provider connection" not in str(error):
                raise
            model = harness._selected_model
        return cls(
            model=model,
            agent=config.agent,
            shell_timeout=config.shell_timeout,
            max_output=config.max_output,
            max_file_bytes=config.max_file_bytes,
            max_tool_rounds=config.max_tool_rounds,
            max_subagent_depth=config.max_subagent_depth,
            provider=config.provider_profile().kind,
            base_url=config.provider_profile().base_url,
            api=config.provider_profile().api,
            auth=config.provider_profile().auth,
            api_key_env=config.provider_profile().key_env,
            compact_trigger=trigger,
            compact_tokens=tokens,
            compact_messages=messages,
            submit_mode=config.submit_mode,
            read_only=config.read_only,
            reasoning_effort=config.reasoning_effort,
        )

    def apply(
        self,
        harness: "Harness",
        *,
        model_binding: ModelBinding = _INHERIT_MODEL,
        default_model_binding: ModelBinding = _INHERIT_MODEL,
    ) -> None:
        # The caller owns Harness.operation and applies provider fields through
        # Harness.reconfigure_provider before calling this for the rest.
        config = harness.config
        config.agent = self.agent
        default = (
            default_model_binding
            if default_model_binding.configured
            else ModelBinding(harness.default_model_binding().binding.provider, self.model)
        )
        harness._selected_model = default.id
        harness._default_provider_name = default.provider
        binding = model_binding if model_binding.configured else harness.resolved_model_binding(self.agent).binding
        config.model = binding.id or self.model
        config.shell_timeout = self.shell_timeout
        config.max_output = self.max_output
        config.max_file_bytes = self.max_file_bytes
        config.max_tool_rounds = self.max_tool_rounds
        config.max_subagent_depth = self.max_subagent_depth
        config.submit_mode = self.submit_mode
        config.reasoning_effort = self.reasoning_effort
        config.read_only = self.read_only or harness._permission_ceiling == "reviewer"
        if harness.agent.provider.model != config.model:
            harness.agent.provider.model = config.model
        harness.agent.max_tool_rounds = self.max_tool_rounds
        _apply_compaction(harness, self.compact_trigger, self.compact_tokens, self.compact_messages)
        harness.refresh_instructions()


class SettingsRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: str = Field(min_length=1, max_length=128)


class SettingsModelReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def complete_reference(self) -> "Self":
        ModelBinding.parse({"provider": self.provider, "id": self.id})
        return self

    def binding(self) -> ModelBinding:
        return ModelBinding(self.provider, self.id)


class SettingsInput(SettingsRevision):
    values: SettingsValues
    model_binding: SettingsModelReference | None = None

    @model_validator(mode="after")
    def matching_model(self) -> "Self":
        if self.model_binding is not None and self.model_binding.id != self.values.model:
            raise ValueError("The selected model reference must match values.model")
        return self


class WebSettings:
    def __init__(self, harness: "Harness") -> None:
        self.harness = harness
        self.catalog_capabilities = CatalogCapabilities()
        # Construct only after initialize(), including initial Codex resolution.
        # Each restart captures the current trusted configuration anew; saved
        # overrides never mutate the administrator's ceiling or storage.
        self._provider_admin = copy.deepcopy(harness.config)
        startup_model = harness._selected_model
        self.defaults = SettingsValues.current(harness).model_copy(
            update={"model": harness.config.global_model_default or startup_model}
        )
        self.startup_defaults = self.defaults
        self._startup_global_model = harness.provider_store.model_store("global").load()
        self._global_startup_model = (
            harness.config.global_model_default or self._startup_global_model or self.defaults.model
        )
        self._pinned_startup_model = harness.config.model_config_explicit
        self._startup_model = startup_model
        self._startup_global_binding = harness.provider_store.model_binding("global")
        self._startup_binding = ModelBinding(
            harness._default_provider_name
            if "" in harness.config.providers
            else harness._default_provider_name or harness.providers.active,
            self.defaults.model,
        )
        self._published_bindings: dict[str, tuple[ModelBinding, str]] = {}
        self._publish_bindings()
        self.global_path = harness.config.data_dir / "web-defaults.db"
        self.global_revision = "0" * 64
        self.global_persisted = False
        self.values = self.defaults
        self.workspace_overrides: dict[str, object] = {}
        self.effective_mode = harness.mode
        self.revision = secrets.token_hex(32)
        self.stored_revision = self.revision
        self.persisted = False
        # Write-only keys entered in the browser, keyed by provider name.
        self.keys: dict[str, str] = {}
        # Original process environment values, restored when a key is removed.
        self._env_backup: dict[str, str] = {}
        self._env_present: set[str] = set()
        self._applied_key_env = ""

    def _scope_binding(self, scope: str) -> tuple[ModelBinding, str]:
        if self._pinned_startup_model:
            return self._startup_binding, "startup"
        store = self.harness.provider_store
        unnamed = not self.harness._default_provider_name and "" in self.harness.config.providers
        if scope == "workspace":
            local = store.model_store("workspace").load_binding() if unnamed else store.model_binding("workspace")
            if local.configured:
                return local, "workspace"
        global_binding = (
            store.model_store("global").load_binding()
            if unnamed and scope == "workspace"
            else store.model_binding("global")
        )
        if global_binding.configured:
            return global_binding, "global"
        if scope == "global":
            return ModelBinding(
                self._startup_global_binding.provider or self._startup_binding.provider,
                self._global_startup_model,
            ), "startup"
        return self._startup_binding, "startup"

    def _publish_bindings(self) -> None:
        self._published_bindings = {scope: self._scope_binding(scope) for scope in ("global", "workspace")}

    def _settings_revision(self, scope: Literal["global", "workspace"]) -> str:
        parts = [self.global_revision if scope == "global" else self.revision]
        layers: tuple[Literal["global", "workspace"], ...] = ("global", "workspace")
        for layer in layers:
            parts.append(agent_document_store(self.harness.provider_store, layer).load("assistant").revision)
            if scope == "global":
                break
        return hashlib.sha256(":".join(parts).encode()).hexdigest()

    def _requested_binding(self, scope: str, values: SettingsValues, binding: ModelBinding) -> ModelBinding:
        if binding.configured and binding.id != values.model:
            raise ValueError("The selected model reference must match values.model")
        selected = binding if binding.configured else ModelBinding(self._scope_binding(scope)[0].provider, values.model)
        if selected.provider and selected.provider not in self.harness.provider_store.load().providers:
            raise ValueError(f"Model references unavailable provider connection {selected.provider!r}")
        return selected

    def _binding_snapshot(self, scope: str) -> dict[str, object]:
        binding, source = self._published_bindings[scope]
        inherited = (
            self._published_bindings["global"][0]
            if scope == "workspace"
            else ModelBinding(
                self._startup_global_binding.provider or self._startup_binding.provider, self._global_startup_model
            )
        )
        registry = self.harness.provider_store.load()
        local = self.harness.provider_store.load_scope("workspace").providers
        available = not binding.provider or binding.provider in registry.providers
        return {
            "model_binding": binding.as_dict(),
            "default_model_binding": inherited.as_dict(),
            "model_binding_source": source,
            "provider_origin": ("workspace" if binding.provider in local else "global")
            if binding.provider in registry.providers
            else "",
            "model_binding_available": available,
            "model_binding_unavailable_reason": ""
            if available
            else f"Provider connection {binding.provider!r} is unavailable. Choose another model connection.",
        }

    def provider_config(
        self,
        values: SettingsValues,
        *,
        model_binding: ModelBinding = _INHERIT_MODEL,
        allow_unavailable: bool = False,
    ) -> "HarnessConfig":
        """Validated candidate configuration with allowlisted overrides applied."""
        candidate = copy.deepcopy(self._provider_admin)
        self.harness.refresh_agent_definitions()
        candidate.profiles = copy.deepcopy(self.harness.config.profiles)
        candidate.agent_sources = self.harness.config.agent_sources
        candidate.agent_source_profiles = copy.deepcopy(self.harness.config.agent_source_profiles)
        profile = candidate.profile(values.agent)
        registry = self.harness.provider_store.load()
        binding = (
            model_binding
            if model_binding.configured
            else ModelBinding(self._scope_binding("workspace")[0].provider, values.model)
        )
        if binding.id != values.model:
            raise ValueError("The selected model reference must match values.model")
        preference = self.harness.provider_store.agent_models.load(values.agent)
        if preference.model_present:
            if preference.binding.configured:
                binding = preference.binding
        elif profile.model or profile.provider:
            binding = ModelBinding(profile.provider or binding.provider, profile.model or binding.id)
        unavailable = bool(binding.provider and binding.provider not in registry.providers)
        if unavailable and not allow_unavailable:
            raise ValueError(f"Model references unavailable provider connection {binding.provider!r}")
        candidate.providers.update(registry.providers)
        candidate.provider = "" if unavailable else binding.provider
        candidate.unavailable_model_binding = binding if unavailable else _INHERIT_MODEL
        candidate.model = binding.id
        candidate.reasoning_effort = values.reasoning_effort
        candidate.agent = values.agent
        candidate.validate()
        return candidate

    def _effective_values(
        self, values: SettingsValues, *, model_binding: ModelBinding = _INHERIT_MODEL
    ) -> SettingsValues:
        candidate = self.provider_config(values, model_binding=model_binding)
        if not candidate.provider:
            return values
        return values.model_copy(
            update={
                "provider": candidate.provider_profile().kind,
                "base_url": candidate.provider_profile().base_url,
                "api": candidate.provider_profile().api,
                "auth": candidate.provider_profile().auth,
                "api_key_env": candidate.provider_profile().key_env,
            }
        )

    async def _apply_values(self, values: SettingsValues, *, allow_unavailable: bool = False) -> None:
        """Apply the accepted default and agent pair without rereading old preferences."""
        default = ModelBinding(self._scope_binding("workspace")[0].provider, values.model)
        candidate = self.provider_config(values, model_binding=default, allow_unavailable=allow_unavailable)
        await self.harness.reconfigure_provider(candidate)
        values.apply(
            self.harness,
            model_binding=ModelBinding(candidate.provider, candidate.model),
            default_model_binding=default,
        )

    def sync_provider(self) -> None:
        """Project a shared provider selection into the existing web settings view."""
        if self.harness.config.unavailable_model_binding.configured:
            self._publish_bindings()
            return
        if self.harness.config.provider:
            # A key entered for an unnamed web override must never become the
            # credential of a subsequently selected named connection.
            self._restore_key()
        self.values = self._effective_values(self.values)
        candidate = self.provider_config(self.values)
        if candidate.provider == self.harness.config.provider:
            self.harness.config.model = candidate.model
            self.harness.agent.provider.model = candidate.model
        self._publish_bindings()

    def _install_key(self, env: str, secret: str) -> None:
        if not secret:
            return
        if env not in self._env_backup:
            self._env_backup[env] = os.environ.get(env, "")
            if env in os.environ:
                self._env_present.add(env)
        os.environ[env] = secret
        self._applied_key_env = env

    def _restore_key(self) -> None:
        env = self._applied_key_env
        self._applied_key_env = ""
        if not env or env not in self._env_backup:
            return
        if env in self._env_present:
            os.environ[env] = self._env_backup[env]
        else:
            os.environ.pop(env, None)
        self._env_backup.pop(env, None)
        self._env_present.discard(env)

    def snapshot(self) -> dict[str, object]:
        config = self.harness.config
        profile = (
            config.profile(self.values.agent)
            if self.values.agent in config.profile_names
            else config.profile("assistant")
        )
        # Model transactions apply their candidate to the runtime before commit.
        # The settings projection must keep its previously committed selection.
        preference = self.harness.provider_store.agent_models.load(self.values.agent)
        if preference.model_present:
            effective_model = preference.binding.id or self.values.model
        else:
            effective_model = profile.model or self.values.model
        return {
            "scope": "workspace",
            **self._binding_snapshot("workspace"),
            "read_only_locked": self._provider_admin.read_only,
            "values": self.values.model_dump(),
            "defaults": self.defaults.model_dump(),
            "profiles": [
                {
                    "name": name,
                    "mode": self.harness.mode_for_profile(name),
                    "model": config.profile(name).model,
                    "reasoning_effort": config.profile(name).reasoning_effort,
                    **({"description": config.profile(name).description} if config.profile(name).description else {}),
                    **(
                        {"source": config.profile(name).source, "source_format": config.profile(name).source_format}
                        if config.profile(name).source
                        else {}
                    ),
                    **({"provider": config.profile(name).provider} if config.profile(name).provider else {}),
                }
                for name in config.selectable_profile_names
            ],
            **({"agent_diagnostics": list(config.agent_diagnostics)} if config.agent_diagnostics else {}),
            "revision": self._settings_revision("workspace"),
            "persisted": self.persisted,
            "effective_mode": self.effective_mode,
            "effective_model": effective_model,
            "effective_reasoning_effort": resolve_effort(profile.reasoning_effort, self.values.reasoning_effort),
            "providers": sorted(PROVIDERS),
            "apis": list(PROVIDER_APIS),
            "auths": list(PROVIDER_AUTHS),
            "connection": {
                **({"provider_id": config.provider} if config.provider else {}),
                "provider": config.provider_profile().kind,
                "api": config.provider_profile().api,
                "auth": config.provider_profile().auth,
                "base_url": config.provider_profile().base_url,
                "api_key_env": config.provider_profile().key_env,
                "key_configured": bool(os.environ.get(config.provider_profile().key_env))
                if config.provider
                else config.provider_profile().kind in self.keys,
                "auth_status": self.harness.auth_status(),
            },
        }

    async def model_catalog(self, name: str, *, scope: str) -> "ModelCatalog":
        """Retain advertised choices only while the accepted route stays unchanged."""
        registry = (
            self.harness.provider_store.load_scope("global")
            if scope == "global"
            else self.harness.provider_store.load()
        )
        profile = registry.providers.get(name)
        if profile is None:
            raise ValueError("Provider connection is unavailable in this scope")
        accepted = catalog_route(profile, self.harness.openai_auth)
        catalog = await self.harness.provider_catalog(name, scope=scope)
        current = (
            self.harness.provider_store.load_scope("global")
            if scope == "global"
            else self.harness.provider_store.load()
        ).providers.get(name)
        if current is not None and accepted == catalog_route(current, self.harness.openai_auth):
            self.catalog_capabilities.remember(scope, name, accepted, catalog)
        return catalog

    def reasoning_options(
        self,
        *,
        scope: str = "workspace",
        provider: str = "",
        model: str = "",
    ) -> dict[str, object]:
        """Read-only capability lookup for a configured provider connection."""
        source = "Known model support"
        selected_model = model or (self.defaults.model if scope == "global" else self.values.model)
        registry = (
            self.harness.provider_store.load_scope("global")
            if scope == "global"
            else self.harness.provider_store.load()
        )
        selected = provider or registry.active
        profile = registry.providers.get(selected)
        if selected and profile is None:
            raise ValueError("Provider connection is unavailable in this scope")
        if profile is None:
            profile = self._provider_admin.provider_profile()
        choices = reasoning_effort_options(
            profile.kind, selected_model, api=profile.api, auth=profile.auth, base_url=profile.base_url
        )
        retained = self.catalog_capabilities.lookup(
            scope, selected, catalog_route(profile, self.harness.openai_auth), selected_model
        )
        if retained.known:
            choices, source = retained.options, "Provider metadata"
        elif not retained.tracked and profile == self.harness._built_provider_profile:
            cached = self.harness.agent.provider.cached_reasoning_efforts(selected_model)
            if cached != choices:
                choices, source = cached, "Provider metadata"
        return {
            "options": list(choices),
            "source": source,
            "provider": provider or profile.kind,
            "model": selected_model,
            "refresh_required": retained.refresh_required,
        }

    async def validate_reasoning(
        self,
        values: SettingsValues,
        *,
        scope: str = "workspace",
        model_binding: ModelBinding = _INHERIT_MODEL,
        catalog_scope: str = "",
    ) -> None:
        if not values.reasoning_effort or self.harness.config.demo:
            return
        binding = self._requested_binding(scope, values, model_binding)
        lookup_scope = catalog_scope or scope
        reply = self.reasoning_options(scope=lookup_scope, provider=binding.provider, model=binding.id)
        options = reply["options"]
        refresh = bool(reply["refresh_required"])
        if isinstance(options, list) and values.reasoning_effort in options and not refresh:
            return
        if reply["source"] == "Provider metadata" and not refresh:
            raise ValueError("This reasoning effort is not supported by the selected provider and model")
        registry = (
            self.harness.provider_store.load_scope("global")
            if lookup_scope == "global"
            else self.harness.provider_store.load()
        )
        if not binding.provider:
            raise ValueError("This reasoning effort is not supported by the selected provider and model")
        try:
            # Save may verify an expired catalog or opaque credential source.
            # Read-only options never initiate an authentication or network call.
            async with asyncio.timeout(10):
                catalog = await self.model_catalog(binding.provider, scope=lookup_scope)
        except Exception:
            raise HTTPException(
                422, "Reasoning options could not be refreshed. Refresh the model catalog or choose Provider default."
            ) from None
        current = (
            self.harness.provider_store.load_scope("global")
            if lookup_scope == "global"
            else self.harness.provider_store.load()
        )
        if current.providers.get(binding.provider) != registry.providers.get(binding.provider):
            raise HTTPException(422, "Provider connection changed. Refresh the model catalog before saving.")
        details = catalog.metadata.get(values.model, {})
        if "reasoning_efforts" in details:
            if values.reasoning_effort in details["reasoning_efforts"]:
                return
        elif (
            reply["source"] == "Known model support"
            and isinstance(options, list)
            and values.reasoning_effort in options
        ):
            return
        raise HTTPException(
            422, "This reasoning effort is unavailable. Refresh the model catalog and choose a supported effort."
        )

    def validate(self, values: SettingsValues, *, model_binding: ModelBinding = _INHERIT_MODEL) -> str:
        self.harness.refresh_agent_definitions()
        if not self.harness.config.profile(values.agent).user_invocable:
            raise ValueError(f"Agent {values.agent!r} is available only for delegation")
        self.provider_config(values, model_binding=model_binding)
        payload = values.model_dump_json()
        if len(payload.encode("utf-8")) > MAX_SETTINGS_BYTES:
            raise ValueError("Settings exceed the storage limit")
        return payload

    async def load(self) -> None:
        await _join(asyncio.create_task(self._load()))

    async def _load(self) -> None:
        try:
            await self.load_global()
        except Exception:
            raise RuntimeError(
                "Cannot load global web defaults. Check web-defaults.db in the configured data directory."
            ) from None
        try:
            async with aiosqlite.connect(self.harness.agent.session.db_path, timeout=5) as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS ngn_web_settings ("
                    "id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL, "
                    "values_json TEXT NOT NULL, revision TEXT NOT NULL)"
                )
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS ngn_web_provider_keys (provider TEXT PRIMARY KEY, secret TEXT NOT NULL)"
                )
                await db.commit()
                # Bound data before bringing it into Python, even if the DB was
                # edited outside the application. Unknown schemas fail closed.
                async with db.execute(
                    "SELECT version, CASE WHEN typeof(values_json) = 'text' "
                    "AND length(CAST(values_json AS BLOB)) <= ? THEN values_json END, revision "
                    "FROM ngn_web_settings WHERE id = 1",
                    (MAX_SETTINGS_BYTES,),
                ) as cursor:
                    row = await cursor.fetchone()
                async with db.execute(
                    "SELECT provider, CASE WHEN typeof(secret) = 'text' "
                    "AND length(CAST(secret AS BLOB)) <= ? THEN secret END "
                    "FROM ngn_web_provider_keys",
                    (MAX_API_KEY_BYTES,),
                ) as cursor:
                    key_rows = await cursor.fetchall()
            keys: dict[str, str] = {}
            for provider, secret in key_rows:
                if (
                    isinstance(provider, str)
                    and provider in PROVIDERS
                    and isinstance(secret, str)
                    and 0 < len(secret.encode("utf-8")) <= MAX_API_KEY_BYTES
                ):
                    keys[provider] = secret
            # Connection credentials belong to named providers. Old unnamed
            # web keys remain stored but are never adopted by this process.
            self.keys = {}
            if row is not None:
                version, payload, revision = row
                if (
                    type(version) is not int
                    or version not in {1, 2, 3, 4, 5}
                    or not isinstance(payload, str)
                    or not isinstance(revision, str)
                    or len(revision) != 64
                    or any(char not in "0123456789abcdef" for char in revision)
                ):
                    raise ValueError("Invalid saved settings")
                differences: dict[str, object] = {}
                if version == 1:
                    legacy = _SettingsValuesV1.model_validate_json(payload)
                    # Only fields absent from older schemas come from trusted
                    # startup defaults. Reject malformed/extra fields first.
                    values = SettingsValues.model_validate({**self.defaults.model_dump(), **legacy.model_dump()})
                elif version == 2:
                    legacy = _SettingsValuesV1.model_validate_json(payload)
                    values = SettingsValues.model_validate({**self.defaults.model_dump(), **legacy.model_dump()})
                elif version == 3:
                    legacy_values = json.loads(payload)
                    if not isinstance(legacy_values, dict) or {"submit_mode", "read_only"} & legacy_values.keys():
                        raise ValueError("Invalid version-3 settings")
                    values = SettingsValues.model_validate(
                        {
                            **legacy_values,
                            "submit_mode": self.defaults.submit_mode,
                            "read_only": self.defaults.read_only,
                        }
                    )
                elif version == 4:
                    values = SettingsValues.model_validate_json(payload)
                else:
                    differences = json.loads(payload)
                    if not isinstance(differences, dict) or set(differences) - set(SettingsValues.model_fields):
                        raise ValueError("Invalid workspace overrides")
                    values = SettingsValues.model_validate({**self.defaults.model_dump(), **differences})
                # Older web defaults used these names for the same unrestricted
                # profile. Explicit custom profiles keep their configured identity.
                if (
                    version < 4
                    and values.agent in {"build", "agent", "reviewer"}
                    and values.agent not in self.harness.config.profiles
                ):
                    values = values.model_copy(
                        update={"agent": "assistant", "read_only": values.read_only or values.agent == "reviewer"}
                    )
                values = values.model_copy(update={name: getattr(self.defaults, name) for name in _CONNECTION_FIELDS})
                differences = {key: value for key, value in differences.items() if key not in _CONNECTION_FIELDS}
                if self._pinned_startup_model:
                    values = values.model_copy(update={"model": self._startup_model})
                    differences.pop("model", None)
                else:
                    model_store = self.harness.provider_store.model_store("workspace")
                    selected_model = self.harness.provider_store.model()
                    values = values.model_copy(update={"model": selected_model or self.defaults.model})
                    local_model = model_store.load()
                    if version == 5 and local_model:
                        differences["model"] = local_model
                # The persisted default agent can differ from the startup
                # agent. Resolve its actual model before deciding whether the
                # runtime needs an unavailable-connection placeholder.
                unavailable = self.provider_config(values, allow_unavailable=True).unavailable_model_binding.configured
                default_unavailable = self.provider_config(
                    self.defaults, allow_unavailable=True
                ).unavailable_model_binding.configured
                effective_defaults = self.defaults if default_unavailable else self._effective_values(self.defaults)
                self.workspace_overrides = (
                    {
                        key: value
                        for key, value in values.model_dump().items()
                        if value != getattr(effective_defaults, key)
                    }
                    if version < 5
                    else differences
                )
                if unavailable:
                    if not self.harness.config.profile(values.agent).user_invocable:
                        raise ValueError("The saved agent is not available for direct selection")
                else:
                    values = self._effective_values(values)
                    self.validate(values)
                with self.harness.operation("load web settings"):
                    await self._apply_values(values, allow_unavailable=True)
                self.values = values
                self.effective_mode = self.harness.mode
                self.revision = revision
                self.stored_revision = revision
                self.persisted = True
            elif self.global_persisted or self.harness.provider_store.model():
                unavailable = self.harness.config.unavailable_model_binding.configured
                if not unavailable:
                    self.defaults = self._effective_values(self.defaults)
                selected_model = (
                    self._startup_model
                    if self._pinned_startup_model
                    else self.harness.provider_store.model() or self.defaults.model
                )
                effective = self.defaults.model_copy(update={"model": selected_model})
                with self.harness.operation("load global defaults"):
                    await self._apply_values(effective, allow_unavailable=True)
                self.values = effective
                self.effective_mode = self.harness.mode
            if self.harness.config.provider:
                self.sync_provider()
            self._publish_bindings()
        except Exception:
            raise RuntimeError(
                "Cannot load saved web settings. Stop ngn and have the administrator repair or remove "
                "the ngn_web_settings row in this workspace's session database."
            ) from None

    async def load_global(self) -> None:
        """Defaults shared by workspaces using the same application data directory."""
        self.defaults = self.startup_defaults.model_copy(update={"model": self._global_startup_model})
        current_model = self.harness.provider_store.model_store("global").load()
        if current_model and not self._pinned_startup_model:
            self.defaults = self.defaults.model_copy(update={"model": current_model})
        self._publish_bindings()
        self.global_revision = "0" * 64
        self.global_persisted = False
        if not self.global_path.is_file():
            return
        async with aiosqlite.connect(self.global_path, timeout=5) as db:
            await db.execute(_GLOBAL_SETTINGS_TABLE)
            await db.commit()
            async with db.execute(
                "SELECT version, CASE WHEN length(CAST(values_json AS BLOB)) <= ? THEN values_json END, revision "
                "FROM ngn_web_global_settings WHERE id = 1",
                (MAX_SETTINGS_BYTES,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return
        version, payload, revision = row
        if (
            version != 1
            or not isinstance(payload, str)
            or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{64}", revision)
        ):
            raise ValueError("Invalid global settings")
        values = json.loads(payload)
        if not isinstance(values, dict) or set(values) - (set(SettingsValues.model_fields) - {"agent"}):
            raise ValueError("Invalid global settings fields")
        values = {key: value for key, value in values.items() if key not in _CONNECTION_FIELDS}
        values.pop("model", None)
        self.defaults = SettingsValues.model_validate({**self.defaults.model_dump(), **values})
        model_store = self.harness.provider_store.model_store("global")
        selected_model = model_store.load()
        if selected_model and not self._pinned_startup_model:
            self.defaults = self.defaults.model_copy(update={"model": selected_model})
        self.global_revision = revision
        self.global_persisted = True
        self._publish_bindings()

    def global_snapshot(self) -> dict[str, object]:
        result = self.snapshot()
        result.update(
            scope="global",
            values=self.defaults.model_dump(),
            defaults=self.startup_defaults.model_copy(update={"model": self._global_startup_model}).model_dump(),
            effective_model=self.defaults.model,
            effective_reasoning_effort=self.defaults.reasoning_effort,
            revision=self._settings_revision("global"),
            persisted=self.global_persisted,
            connection={
                "provider": self.defaults.provider,
                "api": self.defaults.api,
                "auth": self.defaults.auth,
                "base_url": self.defaults.base_url,
                "api_key_env": self.defaults.api_key_env,
                "key_configured": False,
                "auth_status": "Global defaults; credentials resolve in each workspace.",
            },
        )
        result.update(self._binding_snapshot("global"))
        return result

    async def change_global(
        self,
        revision: str,
        values: SettingsValues,
        *,
        reset: bool = False,
        model_binding: ModelBinding = _INHERIT_MODEL,
    ) -> None:
        # Profiles and write-only keys belong to a workspace. Global defaults
        # carry provider credential references, never copied secret values.
        if "reasoning_effort" not in values.model_fields_set:
            values = values.model_copy(update={"reasoning_effort": self.defaults.reasoning_effort})
        if "submit_mode" not in values.model_fields_set:
            values = values.model_copy(update={"submit_mode": self.defaults.submit_mode})
        if "read_only" not in values.model_fields_set:
            values = values.model_copy(update={"read_only": self.defaults.read_only})
        values = values.model_copy(update={"agent": self.startup_defaults.agent})
        if any(getattr(values, name) != getattr(self.defaults, name) for name in _CONNECTION_FIELDS):
            raise HTTPException(422, "Edit connections in Provider connections.")
        if revision != self._settings_revision("global"):
            raise HTTPException(409, "Global settings changed. Refresh before saving.")
        try:
            binding = self._requested_binding("global", values, model_binding)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        if values.model != self.defaults.model and not binding.provider:
            raise HTTPException(422, "Choose a provider connection before changing the model.")
        self.validate(values, model_binding=binding)
        merged_catalog = model_binding.configured or bool(
            self.harness.provider_store.model_store("global").load_binding().provider
        )
        await self.validate_reasoning(
            values,
            scope="global",
            model_binding=binding,
            catalog_scope="workspace" if merged_catalog else "global",
        )
        payload = values.model_dump_json(exclude={"agent", *_CONNECTION_FIELDS})
        next_revision = secrets.token_hex(32)
        self.global_path.parent.mkdir(parents=True, exist_ok=True)
        self.global_path.touch(mode=0o600, exist_ok=True)
        async with aiosqlite.connect(self.global_path, timeout=5) as db:
            await db.execute(_GLOBAL_SETTINGS_TABLE)
            await db.commit()
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT revision FROM ngn_web_global_settings WHERE id = 1") as cursor:
                current = await cursor.fetchone()
            if (current[0] if current else "0" * 64) != self.global_revision:
                raise HTTPException(409, "Global settings changed. Refresh before saving.")
            if reset:
                await db.execute("DELETE FROM ngn_web_global_settings WHERE id = 1")
            else:
                await db.execute(
                    "INSERT INTO ngn_web_global_settings VALUES (1, 1, ?, ?) ON CONFLICT(id) DO UPDATE SET values_json=excluded.values_json, revision=excluded.revision",
                    (payload, next_revision),
                )
            await db.commit()
        global_model_store = self.harness.provider_store.model_store("global")
        if model_binding.configured:
            self._pinned_startup_model = False
            self.harness.config.model_config_explicit = False
            self._provider_admin.model_config_explicit = False
        if not self._pinned_startup_model:
            if reset:
                global_model_store.save_binding(ModelBinding())
            elif binding.provider:
                global_model_store.save_binding(binding)
        await self.load_global()
        if self.persisted:
            inherited = SettingsValues.model_validate({**self.defaults.model_dump(), **self.workspace_overrides})
            inherited = inherited.model_copy(update={"model": self._scope_binding("workspace")[0].id})
            inherited = self._effective_values(inherited)
            with self.harness.operation("apply inherited global defaults"):
                await self._apply_values(inherited)
            self.values = inherited
            self.effective_mode = self.harness.mode
            self.revision = secrets.token_hex(32)
        else:
            effective = self.defaults.model_copy(update={"model": self._scope_binding("workspace")[0].id})
            effective = self._effective_values(effective)
            with self.harness.operation("apply global defaults"):
                await self._apply_values(effective)
            self.values = effective
            self.effective_mode = self.harness.mode
            self.revision = secrets.token_hex(32)
        self._publish_bindings()

    async def change(
        self,
        revision: str,
        values: SettingsValues,
        *,
        reset: bool = False,
        model_binding: ModelBinding = _INHERIT_MODEL,
    ) -> None:
        if any(getattr(values, name) != getattr(self.values, name) for name in _CONNECTION_FIELDS):
            raise HTTPException(422, "Edit connections in Provider connections.")
        if "reasoning_effort" not in values.model_fields_set:
            values = values.model_copy(update={"reasoning_effort": self.values.reasoning_effort})
        if "submit_mode" not in values.model_fields_set:
            values = values.model_copy(update={"submit_mode": self.values.submit_mode})
        if "read_only" not in values.model_fields_set:
            values = values.model_copy(update={"read_only": self.values.read_only})
        if revision != self._settings_revision("workspace"):
            raise HTTPException(409, "Settings changed. Reload settings before saving.")
        try:
            binding = (
                self._published_bindings["global"][0]
                if reset
                else self._requested_binding("workspace", values, model_binding)
            )
            if not reset and values.model != self.values.model and not binding.provider:
                raise ValueError("Choose a provider connection before changing the model.")
            values = self._effective_values(values, model_binding=binding)
            self.validate(values, model_binding=binding)
            await self.validate_reasoning(values, model_binding=binding)
            base = (
                self.defaults
                if self.harness.config.unavailable_model_binding.configured
                else self._effective_values(self.defaults)
            ).model_dump()
            overrides = {key: value for key, value in values.model_dump().items() if base[key] != value}
            payload = json.dumps(overrides, separators=(",", ":"))
        except ValueError as error:
            detail = (
                str(error)
                if str(error).startswith("Model references unavailable provider connection")
                or str(error) == "Choose a provider connection before changing the model."
                else "Invalid settings or unavailable agent profile."
            )
            raise HTTPException(422, detail) from None
        try:
            with self.harness.operation("web settings"):
                await _join(
                    asyncio.create_task(
                        self._save(
                            values,
                            payload,
                            overrides,
                            reset=reset,
                            model_binding=binding,
                            explicit_binding=model_binding.configured,
                        )
                    )
                )
        except RuntimeError:
            raise HTTPException(409, "Harness busy. Cancel or finish the active operation first.") from None

    async def _save(
        self,
        values: SettingsValues,
        payload: str,
        overrides: dict[str, object],
        *,
        reset: bool,
        model_binding: ModelBinding,
        explicit_binding: bool,
    ) -> None:
        harness = self.harness
        config = harness.config
        revision = secrets.token_hex(32)
        provider = values.provider
        try:
            async with aiosqlite.connect(harness.agent.session.db_path, timeout=5) as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute("SELECT revision FROM ngn_web_settings WHERE id = 1") as cursor:
                    row = await cursor.fetchone()
                if (row[0] if row is not None else "") != (self.stored_revision if self.persisted else ""):
                    raise HTTPException(409, "Stored settings changed. Reconnect to the current workspace owner.")
                if reset:
                    await db.execute("DELETE FROM ngn_web_settings WHERE id = 1")
                    await db.execute("DELETE FROM ngn_web_provider_keys")
                else:
                    await db.execute(
                        "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, 5, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET version = 5, values_json = excluded.values_json, "
                        "revision = excluded.revision",
                        (payload, revision),
                    )
                previous = (
                    config.model,
                    config.agent,
                    config.shell_timeout,
                    config.max_output,
                    config.max_file_bytes,
                    config.max_tool_rounds,
                    config.max_subagent_depth,
                    config.submit_mode,
                    config.read_only,
                    config.reasoning_effort,
                    harness._selected_model,
                    harness._default_provider_name,
                )
                previous_keys = dict(self.keys)
                previous_secret = previous_keys.get(config.provider_profile().kind, "")
                previous_env = config.provider_profile().key_env
                model = harness.agent.provider.model
                rounds = harness.agent.max_tool_rounds
                prompt = harness.agent.system_prompt
                try:
                    if reset:
                        self.keys.clear()
                    self._restore_key()
                    candidate = self.provider_config(values, model_binding=model_binding)
                    secret = self.keys.get(provider, "") if not candidate.provider else ""
                    if secret:
                        self._install_key(values.api_key_env, secret)
                    values.apply(
                        harness,
                        model_binding=ModelBinding(candidate.provider, candidate.model),
                        default_model_binding=model_binding,
                    )
                    await db.commit()
                except BaseException:
                    (
                        config.model,
                        config.agent,
                        config.shell_timeout,
                        config.max_output,
                        config.max_file_bytes,
                        config.max_tool_rounds,
                        config.max_subagent_depth,
                        config.submit_mode,
                        config.read_only,
                        config.reasoning_effort,
                        harness._selected_model,
                        harness._default_provider_name,
                    ) = previous
                    harness.agent.provider.model = model
                    harness.agent.max_tool_rounds = rounds
                    harness.agent.system_prompt = prompt
                    self.keys = previous_keys
                    self._restore_key()
                    if previous_secret:
                        self._install_key(previous_env, previous_secret)
                    await db.rollback()
                    raise
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(500, "Settings could not be persisted. Reload settings before trying again.") from None
        # The commit established the saved state. Provider swaps close the
        # previous client, so they happen only after a successful commit; a
        # failure here is repaired by reloading the saved settings.
        try:
            if explicit_binding:
                self._pinned_startup_model = False
                harness.config.model_config_explicit = False
                self._provider_admin.model_config_explicit = False
            if not self._pinned_startup_model:
                store = harness.provider_store.model_store("workspace")
                if reset:
                    store.save_binding(ModelBinding())
                elif model_binding.provider and (explicit_binding or "model" in overrides or store.load()):
                    store.save_binding(model_binding)
            await harness.reconfigure_provider(self.provider_config(values, model_binding=model_binding))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                500, "Settings were saved but could not be applied. Reload settings before continuing."
            ) from None
        # Publish without an await after commit. GETs see the previous
        # committed snapshot throughout the transaction, never a draft.
        self.values = values
        self.workspace_overrides = {} if reset else overrides
        self.effective_mode = harness.mode
        self.revision = revision
        self.stored_revision = revision
        self.persisted = not reset
        self._publish_bindings()
