"""Allowlisted workspace settings; no credentials or trusted configuration writes."""

import asyncio
import copy
import secrets
from typing import TYPE_CHECKING

import aiosqlite
import anyio
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from nagents.harness.dictation import BYTES_PER_SECOND
from nagents.harness.dictation import SAMPLE_RATE
from nagents.harness.dictation import DictationError
from nagents.harness.dictation import VoiceDictation

if TYPE_CHECKING:
    from nagents.harness import Harness
    from nagents.harness.config import HarnessConfig

MAX_SETTINGS_BYTES = 16384


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
    dictation_enabled: bool
    dictation_model: str = Field(min_length=1, max_length=200)
    dictation_language: str = Field(pattern=r"^(?:[a-z]{2})?$")
    dictation_max_seconds: int = Field(ge=1, le=300)

    @field_validator("dictation_model", mode="before")
    @classmethod
    def dictation_model_id(cls, value: object) -> object:
        return cls.model_id(value)

    @classmethod
    def current(cls, harness: "Harness") -> "SettingsValues":
        config = harness.config
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
        )

    def apply(self, harness: "Harness") -> None:
        # The caller owns Harness.operation. Keep its provider's shared config
        # object intact, and apply the explicit model after selecting the profile.
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
        harness.refresh_instructions()


class SettingsRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: str = Field(min_length=1, max_length=128)


class SettingsInput(SettingsRevision):
    values: SettingsValues


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
        # Web preferences never mutate the administrator's opt-in, ceiling or
        # routing. Each restart captures the current trusted configuration anew.
        self._dictation_admin = copy.deepcopy(harness.config)
        self.defaults = SettingsValues.current(harness)
        self.values = self.defaults
        self.effective_mode = harness.mode
        self.revision = secrets.token_hex(32)
        self.persisted = False

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
            "connection": {
                "provider": config.provider,
                "api": config.api,
                "auth_status": self.harness.auth_status(),
            },
        }

    def validate(self, values: SettingsValues) -> str:
        self.harness.config.profile(values.agent)
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
            if row is not None:
                version, payload, revision = row
                if (
                    type(version) is not int
                    or version not in {1, 2}
                    or not isinstance(payload, str)
                    or not isinstance(revision, str)
                    or len(revision) != 64
                    or any(char not in "0123456789abcdef" for char in revision)
                ):
                    raise ValueError("Invalid saved settings")
                if version == 1:
                    legacy = _SettingsValuesV1.model_validate_json(payload)
                    # Only the four new preferences come from trusted startup
                    # defaults. Reject malformed/extra v1 fields before combining.
                    values = SettingsValues.model_validate({**self.defaults.model_dump(), **legacy.model_dump()})
                else:
                    values = SettingsValues.model_validate_json(payload)
                self.validate(values)
                with self.harness.operation("load web settings"):
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

    async def change(self, revision: str, values: SettingsValues, *, reset: bool = False) -> None:
        try:
            payload = self.validate(values)
        except ValueError:
            raise HTTPException(422, "Invalid settings or unavailable agent profile.") from None
        if revision != self.revision:
            raise HTTPException(409, "Settings changed. Reload settings before saving.")
        try:
            with self.harness.operation("web settings"):
                await _join(asyncio.create_task(self._save(values, payload, reset=reset)))
        except RuntimeError:
            raise HTTPException(409, "Harness busy. Cancel or finish the active operation first.") from None

    async def _save(self, values: SettingsValues, payload: str, *, reset: bool) -> None:
        harness = self.harness
        config = harness.config
        revision = secrets.token_hex(32)
        try:
            async with aiosqlite.connect(harness.agent.session.db_path, timeout=5) as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute("SELECT revision FROM ngn_web_settings WHERE id = 1") as cursor:
                    row = await cursor.fetchone()
                if (row[0] if row is not None else "") != (self.revision if self.persisted else ""):
                    raise HTTPException(409, "Stored settings changed. Reconnect to the current workspace owner.")
                if reset:
                    await db.execute("DELETE FROM ngn_web_settings WHERE id = 1")
                else:
                    await db.execute(
                        "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, 2, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET version = 2, values_json = excluded.values_json, "
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
                )
                model = harness.agent.provider.model
                rounds = harness.agent.max_tool_rounds
                prompt = harness.agent.system_prompt
                try:
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
                    await db.rollback()
                    raise
                # Publish without an await after commit. GETs see the previous
                # committed snapshot throughout the transaction, never a draft.
                self.values = values
                self.effective_mode = harness.mode
                self.revision = revision
                self.persisted = not reset
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(500, "Settings could not be persisted. Reload settings before trying again.") from None
