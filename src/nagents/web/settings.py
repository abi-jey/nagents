"""Allowlisted workspace settings; provider overrides with write-only keys.

Trusted startup configuration remains the source of defaults and the ceiling.
Saved workspace overrides may select an allowlisted provider, endpoint and API
key environment name. A key entered in the browser is stored outside the
settings JSON, is never returned by any endpoint, and is installed only into
this process's environment before the provider is (re)built.
"""

import asyncio
import copy
import os
import re
import secrets
from typing import TYPE_CHECKING

import aiosqlite
import anyio
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from nagents.compactor import Messages
from nagents.compactor import Tokens
from nagents.harness.config import API_NAMES
from nagents.harness.config import PROVIDERS
from nagents.harness.dictation import BYTES_PER_SECOND
from nagents.harness.dictation import SAMPLE_RATE
from nagents.harness.dictation import DictationError
from nagents.harness.dictation import VoiceDictation

if TYPE_CHECKING:
    from nagents.harness import Harness
    from nagents.harness.config import HarnessConfig

MAX_SETTINGS_BYTES = 16384
MAX_API_KEY_BYTES = 4096
PROVIDER_APIS: tuple[str, ...] = tuple(name for name in API_NAMES if name != "completions")
PROVIDER_AUTHS: tuple[str, ...] = ("auto", "api-key", "chatgpt")
COMPACTION_TRIGGERS: tuple[str, ...] = ("auto", "tokens", "messages", "off")
DEFAULT_COMPACT_TOKENS = 200_000
DEFAULT_COMPACT_MESSAGES = 100
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


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


class _SettingsValuesV2(_SettingsValuesV1):
    """Workspace preferences before provider overrides existed."""

    dictation_enabled: bool
    dictation_model: str = Field(min_length=1, max_length=200)
    dictation_language: str = Field(pattern=r"^(?:[a-z]{2})?$")
    dictation_max_seconds: int = Field(ge=1, le=300)

    @field_validator("dictation_model", mode="before")
    @classmethod
    def dictation_model_id(cls, value: object) -> object:
        return cls.model_id(value)


class SettingsValues(_SettingsValuesV2):
    """Current persisted schema, including allowlisted provider overrides."""

    provider: str = Field(min_length=1, max_length=40)
    base_url: str = Field(max_length=300)
    api: str = Field(min_length=1, max_length=20)
    auth: str = Field(min_length=1, max_length=20)
    api_key_env: str = Field(min_length=1, max_length=64)
    compact_trigger: str = Field(default="auto", min_length=1, max_length=20)
    compact_tokens: int = Field(default=DEFAULT_COMPACT_TOKENS, ge=1024, le=10_000_000)
    compact_messages: int = Field(default=DEFAULT_COMPACT_MESSAGES, ge=1, le=10_000)

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
            model=harness.agent.provider.model,
            agent=config.agent,
            shell_timeout=config.shell_timeout,
            max_output=config.max_output,
            max_file_bytes=config.max_file_bytes,
            max_tool_rounds=config.max_tool_rounds,
            max_subagent_depth=config.max_subagent_depth,
            dictation_enabled=config.dictation_enabled,
            dictation_model=config.dictation_model,
            dictation_language=config.dictation_language,
            dictation_max_seconds=config.dictation_max_seconds,
            provider=config.provider,
            base_url=config.base_url,
            api=config.api,
            auth=config.auth,
            api_key_env=config.api_key_env,
            compact_trigger=trigger,
            compact_tokens=tokens,
            compact_messages=messages,
        )

    def apply(self, harness: "Harness") -> None:
        # The caller owns Harness.operation and applies provider fields through
        # Harness.reconfigure_provider before calling this for the rest.
        config = harness.config
        config.agent = self.agent
        config.model = self.model
        config.shell_timeout = self.shell_timeout
        config.max_output = self.max_output
        config.max_file_bytes = self.max_file_bytes
        config.max_tool_rounds = self.max_tool_rounds
        config.max_subagent_depth = self.max_subagent_depth
        harness.agent.provider.model = self.model
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


async def _join(task: asyncio.Task[None]) -> None:
    # Never cancel an aiosqlite commit/connection setup halfway through. The
    # worker does only bounded local SQL (including a five-second busy timeout).
    # Join even repeated asyncio cancellation and AnyIO disconnect cancellation.
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
    if cancelled:
        raise asyncio.CancelledError


class WebSettings:
    def __init__(self, harness: "Harness") -> None:
        self.harness = harness
        # Construct only after initialize(), including initial Codex resolution.
        # Each restart captures the current trusted configuration anew; saved
        # overrides never mutate the administrator's opt-in, ceiling or storage.
        self._dictation_admin = copy.deepcopy(harness.config)
        self._provider_admin = copy.deepcopy(harness.config)
        self.defaults = SettingsValues.current(harness)
        self.values = self.defaults
        self.effective_mode = harness.mode
        self.revision = secrets.token_hex(32)
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
        candidate.model = values.model
        candidate.validate()
        return candidate

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

    def dictation_config(self) -> "HarnessConfig":
        """Detached service configuration using only committed web preferences."""
        config = copy.deepcopy(self._dictation_admin)
        values = self.values
        config.dictation_enabled = config.dictation_enabled and values.dictation_enabled and not config.demo
        config.dictation_model = values.dictation_model
        config.dictation_language = values.dictation_language
        config.dictation_max_seconds = min(values.dictation_max_seconds, config.dictation_max_seconds)
        return config

    def dictation_snapshot(self) -> dict[str, object]:
        """Safe local readiness projection; no device probing or provider I/O."""
        config = self.dictation_config()
        available = False
        if not self._dictation_admin.dictation_enabled:
            status = "Dictation is disabled by the administrator. Ask them to enable dictation and restart ngn."
        elif config.demo:
            status = "Dictation is unavailable in demo mode. Restart without --demo."
        elif not config.dictation_enabled:
            status = "Dictation is disabled in web settings. Enable it and save settings to use voice input."
        else:
            try:
                VoiceDictation(config).check_ready()
            except DictationError as error:
                status = str(error)
            else:
                available = True
                status = "Dictation is ready. Transcription uses a separate API key and API billing."
        return {
            "enabled": config.dictation_enabled,
            "available": available,
            "admin_enabled": self._dictation_admin.dictation_enabled,
            "status": status,
            "api_key_env": config.dictation_api_key_env,
            "max_seconds": config.dictation_max_seconds,
            "max_bytes": config.dictation_max_seconds * BYTES_PER_SECOND + 4096,
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "sample_width": 2,
            "content_type": "audio/wav",
            "revision": self.revision,
        }

    def snapshot(self) -> dict[str, object]:
        config = self.harness.config
        return {
            "values": self.values.model_dump(),
            "defaults": self.defaults.model_dump(),
            "profiles": [
                {"name": name, "mode": config.profile(name).mode, "model": config.profile(name).model}
                for name in ("build", "agent", "reviewer", *sorted(config.profiles))
            ],
            "revision": self.revision,
            "persisted": self.persisted,
            "effective_mode": self.effective_mode,
            "dictation": self.dictation_snapshot(),
            "providers": sorted(PROVIDERS),
            "apis": list(PROVIDER_APIS),
            "auths": list(PROVIDER_AUTHS),
            "connection": {
                "provider": config.provider,
                "api": config.api,
                "auth": config.auth,
                "base_url": config.base_url,
                "api_key_env": config.api_key_env,
                "key_configured": config.provider in self.keys,
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
                    or version not in {1, 2, 3}
                    or not isinstance(payload, str)
                    or not isinstance(revision, str)
                    or len(revision) != 64
                    or any(char not in "0123456789abcdef" for char in revision)
                ):
                    raise ValueError("Invalid saved settings")
                if version == 1:
                    legacy = _SettingsValuesV1.model_validate_json(payload)
                    # Only fields absent from older schemas come from trusted
                    # startup defaults. Reject malformed/extra fields first.
                    values = SettingsValues.model_validate({**self.defaults.model_dump(), **legacy.model_dump()})
                elif version == 2:
                    legacy = _SettingsValuesV2.model_validate_json(payload)
                    values = SettingsValues.model_validate({**self.defaults.model_dump(), **legacy.model_dump()})
                else:
                    values = SettingsValues.model_validate_json(payload)
                self.validate(values)
                secret = keys.get(values.provider, "")
                if secret:
                    self._install_key(values.api_key_env, secret)
                with self.harness.operation("load web settings"):
                    await self.harness.reconfigure_provider(self.provider_config(values))
                    values.apply(self.harness)
                self.values = values
                self.effective_mode = self.harness.mode
                self.revision = revision
                self.persisted = True
        except Exception:
            raise RuntimeError(
                "Cannot load saved web settings. Stop ngn and have the administrator repair or remove "
                "the ngn_web_settings row in this workspace's session database."
            ) from None

    async def change(
        self,
        revision: str,
        values: SettingsValues,
        *,
        api_key: str = "",
        clear_api_key: bool = False,
        reset: bool = False,
    ) -> None:
        try:
            payload = self.validate(values)
        except ValueError:
            raise HTTPException(422, "Invalid settings or unavailable agent profile.") from None
        if revision != self.revision:
            raise HTTPException(409, "Settings changed. Reload settings before saving.")
        try:
            with self.harness.operation("web settings"):
                await _join(
                    asyncio.create_task(
                        self._save(values, payload, api_key=api_key, clear_api_key=clear_api_key, reset=reset)
                    )
                )
        except RuntimeError:
            raise HTTPException(409, "Harness busy. Cancel or finish the active operation first.") from None

    async def _save(
        self,
        values: SettingsValues,
        payload: str,
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
                if (row[0] if row is not None else "") != (self.revision if self.persisted else ""):
                    raise HTTPException(409, "Stored settings changed. Reconnect to the current workspace owner.")
                if reset:
                    await db.execute("DELETE FROM ngn_web_settings WHERE id = 1")
                    await db.execute("DELETE FROM ngn_web_provider_keys")
                else:
                    await db.execute(
                        "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, 3, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET version = 3, values_json = excluded.values_json, "
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
                    secret = self.keys.get(provider, "")
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
        self.values = values
        self.effective_mode = harness.mode
        self.revision = revision
        self.persisted = not reset
