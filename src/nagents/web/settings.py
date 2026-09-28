"""Global defaults and workspace overrides; workspace-scoped write-only keys.

Trusted startup configuration remains the source of defaults and the ceiling.
Saved workspace overrides may select an allowlisted provider, endpoint and API
key environment name. A key entered in the browser is stored outside the
settings JSON, is never returned by any endpoint, and is installed only into
this process's environment before the provider is (re)built.
"""

import asyncio
import copy
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

from nagents.compactor import Messages
from nagents.compactor import Tokens
from nagents.harness.config import API_NAMES
from nagents.harness.config import PROVIDERS

from ._async import join_owned as _join

if TYPE_CHECKING:
    from nagents.harness import Harness
    from nagents.harness.config import HarnessConfig

MAX_SETTINGS_BYTES = 16384
MAX_API_KEY_BYTES = 4096
PROVIDER_APIS: tuple[str, ...] = tuple(name for name in API_NAMES if name != "completions")
PROVIDER_AUTHS: tuple[str, ...] = ("auto", "api-key", "chatgpt", "codex", "entra")
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
    if harness.agent.compactor is None:
        return "off", DEFAULT_COMPACT_TOKENS, DEFAULT_COMPACT_MESSAGES
    configured = harness.agent.compact_on
    if isinstance(configured, Messages):
        return "messages", DEFAULT_COMPACT_TOKENS, configured.length
    if isinstance(configured, Tokens):
        window = configured.total or configured.input or DEFAULT_COMPACT_TOKENS
        return "tokens", window, DEFAULT_COMPACT_MESSAGES
    return "auto", DEFAULT_COMPACT_TOKENS, DEFAULT_COMPACT_MESSAGES


def _apply_compaction(harness: "Harness", trigger: str, tokens: int, messages: int) -> None:
    """Apply a validated criteria choice to the shared live agent."""
    if trigger == "off":
        harness.agent.compactor = None
        harness.agent.compact_on = None
        return
    harness.agent.compactor = "self"
    if trigger == "tokens":
        harness.agent.compact_on = Tokens(total=tokens)
    elif trigger == "messages":
        harness.agent.compact_on = Messages(length=messages)
    else:
        # Provider default: context-limit-based, resolved per run.
        harness.agent.compact_on = None


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
        return cls(
            model=harness._selected_model if config.profile(config.agent).model else harness.agent.provider.model,
            agent=config.agent,
            shell_timeout=config.shell_timeout,
            max_output=config.max_output,
            max_file_bytes=config.max_file_bytes,
            max_tool_rounds=config.max_tool_rounds,
            max_subagent_depth=config.max_subagent_depth,
            provider=config.provider,
            base_url=config.base_url,
            api=config.api,
            auth=config.auth,
            api_key_env=config.api_key_env,
            compact_trigger=trigger,
            compact_tokens=tokens,
            compact_messages=messages,
            submit_mode=config.submit_mode,
            read_only=config.read_only,
        )

    def apply(self, harness: "Harness") -> None:
        # The caller owns Harness.operation and applies provider fields through
        # Harness.reconfigure_provider before calling this for the rest.
        config = harness.config
        config.agent = self.agent
        harness._selected_model = self.model
        config.model = config.profile(self.agent).model or self.model
        config.shell_timeout = self.shell_timeout
        config.max_output = self.max_output
        config.max_file_bytes = self.max_file_bytes
        config.max_tool_rounds = self.max_tool_rounds
        config.max_subagent_depth = self.max_subagent_depth
        config.submit_mode = self.submit_mode
        config.read_only = self.read_only or harness._permission_ceiling == "reviewer"
        harness.agent.provider.model = config.model
        harness.agent.max_tool_rounds = self.max_tool_rounds
        _apply_compaction(harness, self.compact_trigger, self.compact_tokens, self.compact_messages)
        harness.refresh_instructions()


class SettingsRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: str = Field(min_length=1, max_length=128)


class SettingsInput(SettingsRevision):
    values: SettingsValues
    # Write-only provider credential. Empty leaves any stored key unchanged;
    # clear_api_key removes the stored key explicitly. Neither value is ever
    # persisted in the settings JSON or returned by an endpoint.
    api_key: str = Field(default="", max_length=MAX_API_KEY_BYTES)
    clear_api_key: bool = False

    @field_validator("api_key")
    @classmethod
    def provider_key(cls, value: object) -> object:
        if isinstance(value, str) and any(not char.isprintable() for char in value):
            raise ValueError("API key must be printable text")
        return value


class WebSettings:
    def __init__(self, harness: "Harness") -> None:
        self.harness = harness
        # Construct only after initialize(), including initial Codex resolution.
        # Each restart captures the current trusted configuration anew; saved
        # overrides never mutate the administrator's ceiling or storage.
        self._provider_admin = copy.deepcopy(harness.config)
        self.defaults = SettingsValues.current(harness)
        self.startup_defaults = self.defaults
        self._startup_global_model = harness.provider_store.model_store("global").load()
        self._global_startup_model = (
            harness.config.global_model_default or self._startup_global_model or self.defaults.model
        )
        self._pinned_startup_model = harness.config.model_config_explicit
        self._startup_model = harness.config.model
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

    def provider_config(self, values: SettingsValues) -> "HarnessConfig":
        """Validated candidate configuration with allowlisted overrides applied."""
        candidate = copy.deepcopy(self._provider_admin)
        candidate.provider = values.provider
        candidate.base_url = values.base_url
        candidate.api = values.api
        candidate.auth = values.auth
        candidate.api_key_env = values.api_key_env
        candidate.model = candidate.profile(values.agent).model or values.model
        registry = self.harness.provider_store.load()
        name = self.harness.config.profile(values.agent).provider or registry.active
        if name:
            profile = registry.providers[name]
            candidate.provider_id = name
            candidate.provider = profile.kind
            candidate.base_url = profile.base_url
            candidate.api = profile.api
            candidate.auth = profile.auth
            candidate.api_key_env = profile.key_env
            candidate.api_version = profile.api_version
        candidate.validate()
        return candidate

    def _effective_values(self, values: SettingsValues) -> SettingsValues:
        candidate = self.provider_config(values)
        if not candidate.provider_id:
            return values
        return values.model_copy(
            update={
                "provider": candidate.provider,
                "base_url": candidate.base_url,
                "api": candidate.api,
                "auth": candidate.auth,
                "api_key_env": candidate.api_key_env,
            }
        )

    def sync_provider(self) -> None:
        """Project a shared provider selection into the existing web settings view."""
        if self.harness.config.provider_id:
            # A key entered for an unnamed web override must never become the
            # credential of a subsequently selected named connection.
            self._restore_key()
        self.values = self._effective_values(self.values)
        self.harness.config.model = self.harness.config.profile(self.values.agent).model or self.values.model
        self.harness.agent.provider.model = self.harness.config.model

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
        return {
            "scope": "workspace",
            "read_only_locked": self._provider_admin.read_only,
            "values": self.values.model_dump(),
            "defaults": self.defaults.model_dump(),
            "profiles": [
                {
                    "name": name,
                    "mode": self.harness.mode_for_profile(name),
                    "model": config.profile(name).model,
                    **({"provider": config.profile(name).provider} if config.profile(name).provider else {}),
                }
                for name in config.profile_names
            ],
            "revision": self.revision,
            "persisted": self.persisted,
            "effective_mode": self.effective_mode,
            "effective_model": config.profile(self.values.agent).model or self.values.model,
            "providers": sorted(PROVIDERS),
            "apis": list(PROVIDER_APIS),
            "auths": list(PROVIDER_AUTHS),
            "connection": {
                **({"provider_id": config.provider_id} if config.provider_id else {}),
                "provider": config.provider,
                "api": config.api,
                "auth": config.auth,
                "base_url": config.base_url,
                "api_key_env": config.api_key_env,
                "key_configured": bool(os.environ.get(config.api_key_env))
                if config.provider_id
                else config.provider in self.keys,
                "auth_status": self.harness.auth_status(),
            },
        }

    def validate(self, values: SettingsValues) -> str:
        self.harness.config.profile(values.agent)
        self.provider_config(values)
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
            self.keys = keys
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
                if self._pinned_startup_model:
                    values = values.model_copy(update={"model": self._startup_model})
                    differences.pop("model", None)
                else:
                    model_store = self.harness.provider_store.model_store("workspace")
                    if (version < 5 and values.model != self.defaults.model) or "model" in differences:
                        model_store.save(values.model, only_if_missing=True)
                    selected_model = self.harness.provider_store.model()
                    if selected_model:
                        values = values.model_copy(update={"model": selected_model})
                    local_model = model_store.load()
                    if version == 5 and local_model:
                        differences["model"] = local_model
                effective_defaults = self._effective_values(self.defaults)
                self.workspace_overrides = (
                    {
                        key: value
                        for key, value in values.model_dump().items()
                        if value != getattr(effective_defaults, key)
                    }
                    if version < 5
                    else differences
                )
                values = self._effective_values(values)
                self.validate(values)
                secret = keys.get(values.provider, "") if not self.provider_config(values).provider_id else ""
                if secret:
                    self._install_key(values.api_key_env, secret)
                with self.harness.operation("load web settings"):
                    await self.harness.reconfigure_provider(self.provider_config(values))
                    values.apply(self.harness)
                self.values = values
                self.effective_mode = self.harness.mode
                self.revision = revision
                self.stored_revision = revision
                self.persisted = True
            elif self.global_persisted or self.harness.provider_store.model():
                self.defaults = self._effective_values(self.defaults)
                selected_model = (
                    self._startup_model
                    if self._pinned_startup_model
                    else self.harness.provider_store.model() or self.defaults.model
                )
                effective = self.defaults.model_copy(update={"model": selected_model})
                with self.harness.operation("load global defaults"):
                    await self.harness.reconfigure_provider(self.provider_config(effective))
                    effective.apply(self.harness)
                self.values = effective
                self.effective_mode = self.harness.mode
            if self.harness.config.provider_id:
                self.sync_provider()
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
        if self._pinned_startup_model:
            values.pop("model", None)
        self.defaults = SettingsValues.model_validate({**self.defaults.model_dump(), **values})
        model_store = self.harness.provider_store.model_store("global")
        if "model" in values and not self._pinned_startup_model:
            model_store.save(self.defaults.model, only_if_missing=True)
        selected_model = model_store.load()
        if selected_model and not self._pinned_startup_model:
            self.defaults = self.defaults.model_copy(update={"model": selected_model})
        self.validate(self.defaults)
        self.global_revision = revision
        self.global_persisted = True

    def global_snapshot(self) -> dict[str, object]:
        result = self.snapshot()
        result.update(
            scope="global",
            values=self.defaults.model_dump(),
            defaults=self.startup_defaults.model_copy(update={"model": self._global_startup_model}).model_dump(),
            effective_model=self.defaults.model,
            revision=self.global_revision,
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
        return result

    async def change_global(self, revision: str, values: SettingsValues, *, reset: bool = False) -> None:
        # Profiles and write-only keys belong to a workspace. Global defaults
        # carry provider credential references, never copied secret values.
        if "submit_mode" not in values.model_fields_set:
            values = values.model_copy(update={"submit_mode": self.defaults.submit_mode})
        if "read_only" not in values.model_fields_set:
            values = values.model_copy(update={"read_only": self.defaults.read_only})
        values = values.model_copy(update={"agent": self.startup_defaults.agent})
        self.validate(values)
        payload = values.model_dump_json(exclude={"agent"})
        next_revision = secrets.token_hex(32)
        self.global_path.parent.mkdir(parents=True, exist_ok=True)
        self.global_path.touch(mode=0o600, exist_ok=True)
        async with aiosqlite.connect(self.global_path, timeout=5) as db:
            await db.execute(_GLOBAL_SETTINGS_TABLE)
            await db.commit()
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT revision FROM ngn_web_global_settings WHERE id = 1") as cursor:
                current = await cursor.fetchone()
            if (current[0] if current else "0" * 64) != revision:
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
        if not self._pinned_startup_model:
            if reset:
                global_model_store.save(self._startup_global_model)
            else:
                global_model_store.save(values.model)
        await self.load_global()
        if self.persisted:
            inherited = SettingsValues.model_validate({**self.defaults.model_dump(), **self.workspace_overrides})
            inherited = self._effective_values(inherited)
            with self.harness.operation("apply inherited global defaults"):
                await self.harness.reconfigure_provider(self.provider_config(inherited))
                inherited.apply(self.harness)
            self.values = inherited
            self.effective_mode = self.harness.mode
            self.revision = secrets.token_hex(32)
        else:
            with self.harness.operation("apply global defaults"):
                await self.harness.reconfigure_provider(self.provider_config(self.defaults))
                self.defaults.apply(self.harness)
            self.values = self.defaults
            self.effective_mode = self.harness.mode
            self.revision = secrets.token_hex(32)

    async def change(
        self,
        revision: str,
        values: SettingsValues,
        *,
        api_key: str = "",
        clear_api_key: bool = False,
        reset: bool = False,
    ) -> None:
        if self.harness.provider_store.load().active and (api_key or clear_api_key):
            raise HTTPException(422, "Named connections use environment variables; no API keys are saved here.")
        if "submit_mode" not in values.model_fields_set:
            values = values.model_copy(update={"submit_mode": self.values.submit_mode})
        if "read_only" not in values.model_fields_set:
            values = values.model_copy(update={"read_only": self.values.read_only})
        try:
            values = self._effective_values(values)
            self.validate(values)
            base = self._effective_values(self.defaults).model_dump()
            overrides = {key: value for key, value in values.model_dump().items() if base[key] != value}
            payload = json.dumps(overrides, separators=(",", ":"))
        except ValueError:
            raise HTTPException(422, "Invalid settings or unavailable agent profile.") from None
        if revision != self.revision:
            raise HTTPException(409, "Settings changed. Reload settings before saving.")
        try:
            with self.harness.operation("web settings"):
                await _join(
                    asyncio.create_task(
                        self._save(
                            values, payload, overrides, api_key=api_key, clear_api_key=clear_api_key, reset=reset
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
        api_key: str,
        clear_api_key: bool,
        reset: bool,
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
                    if api_key:
                        await db.execute(
                            "INSERT INTO ngn_web_provider_keys (provider, secret) VALUES (?, ?) "
                            "ON CONFLICT(provider) DO UPDATE SET secret = excluded.secret",
                            (provider, api_key),
                        )
                    elif clear_api_key:
                        await db.execute("DELETE FROM ngn_web_provider_keys WHERE provider = ?", (provider,))
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
                    harness._selected_model,
                )
                previous_keys = dict(self.keys)
                previous_secret = previous_keys.get(config.provider, "")
                previous_env = config.api_key_env
                model = harness.agent.provider.model
                rounds = harness.agent.max_tool_rounds
                prompt = harness.agent.system_prompt
                try:
                    if reset:
                        self.keys.clear()
                    elif api_key:
                        self.keys[provider] = api_key
                    elif clear_api_key:
                        self.keys.pop(provider, None)
                    self._restore_key()
                    secret = self.keys.get(provider, "") if not self.provider_config(values).provider_id else ""
                    if secret:
                        self._install_key(values.api_key_env, secret)
                    values.apply(harness)
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
                        harness._selected_model,
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
            await harness.reconfigure_provider(self.provider_config(values))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                500, "Settings were saved but could not be applied. Reload settings before continuing."
            ) from None
        # Publish without an await after commit. GETs see the previous
        # committed snapshot throughout the transaction, never a draft.
        if not self._pinned_startup_model:
            harness.provider_store.model_store("workspace").save(
                values.model if not reset and "model" in overrides else ""
            )
        self.values = values
        self.workspace_overrides = {} if reset else overrides
        self.effective_mode = harness.mode
        self.revision = revision
        self.stored_revision = revision
        self.persisted = not reset
