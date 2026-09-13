"""Explicit installed-plugin discovery and atomic, private connection configuration."""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
import tempfile
from dataclasses import dataclass
from dataclasses import field
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue

from nagents.channels.runtime import _bind
from nagents.channels.runtime import _object_copy
from nagents.channels.runtime import _validate_schema
from nagents.channels.types import Channel
from nagents.channels.types import ChannelPlugin
from nagents.channels.types import ChannelValue

from .channel_privacy import CredentialGuard
from .channel_privacy import CredentialProtectionError

if TYPE_CHECKING:
    from nagents.channels.types import ChannelFactory


class ChannelRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    revision: str = Field(min_length=1, max_length=128)


class ConnectionInput(ChannelRevision):
    plugin: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")
    enabled: bool
    config: dict[str, JsonValue] = Field(default_factory=dict)
    secrets: dict[str, JsonValue] = Field(default_factory=dict)
    main_session_id: str = Field(default="", max_length=80, pattern=r"^(?:ngn-[A-Za-z0-9-]+)?$")


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    plugin: str
    enabled: bool
    config: dict[str, JsonValue]
    secrets: dict[str, JsonValue]
    main_session_id: str


class SavedCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = 1
    revision: str
    connections: dict[str, Connection]


@dataclass
class Plugin:
    entry: metadata.EntryPoint
    name: str
    description: str = ""
    version: str = ""
    schema: dict[str, ChannelValue] = field(default_factory=lambda: {"type": "object"})
    factory: ChannelFactory | None = None

    def public_data(self) -> dict[str, ChannelValue]:
        return {"name": self.name, "description": self.description, "version": self.version, "schema": self.schema}

    def load(self) -> ChannelFactory:
        if self.factory is None:
            factory = self.entry.load()
            if not callable(factory):
                raise ValueError("Channel entry point must be callable")
            if isinstance(factory, ChannelPlugin):
                self.name = factory.name
                self.description = factory.description
                self.schema = _object_copy(factory.config_schema)
            self.factory = cast("ChannelFactory", factory)
        return self.factory

    def secret_fields(self) -> set[str]:
        def private(schema: ChannelValue) -> bool:
            if isinstance(schema, list):
                return any(private(value) for value in schema)
            return isinstance(schema, dict) and (
                schema.get("writeOnly") is True or any(private(value) for value in schema.values())
            )

        properties = self.schema.get("properties", {})
        return {key for key, value in properties.items() if private(value)} if isinstance(properties, dict) else set()


class ChannelCatalog:
    def __init__(self, data_dir: Path, session_db: Path, *, allow_plugins: bool = True) -> None:
        self.allow_plugins = allow_plugins
        self.plugin_path = (
            Path(os.environ.get("NGN_CHANNEL_PLUGIN_PATH") or data_dir / "channel-plugins").expanduser().resolve()
        )
        # The existing Harness file-tool storage guard protects the entire data dir,
        # including filesystem aliases; credentials also use a protected basename.
        self.path = session_db.parent / "channels" / "credentials.json"
        self.revision = secrets.token_hex(32)
        self.connections: dict[str, Connection] = {}
        self.plugins: dict[str, Plugin] = {}
        self.collisions: set[str] = set()
        self.status: dict[str, tuple[str, str]] = {}
        self.protection = CredentialGuard()

    def initialize(self) -> None:
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError("Channel credential storage must not be a symlink")
        if self.allow_plugins:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            self.plugin_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            if self.allow_plugins:
                self.path.chmod(0o600)
            if self.path.stat().st_size > 1024 * 1024:
                raise ValueError("Channel configuration exceeds the storage limit")
            try:
                saved = SavedCatalog.model_validate_json(self.path.read_bytes())
            except Exception:
                raise ValueError("Saved channel configuration is invalid") from None
            if saved.version != 1:
                raise ValueError("Unsupported channel configuration version")
            self.connections = saved.connections
            self.revision = saved.revision
        self.remember_credentials(self.connections)
        self.discover()

    def discover(self, *, descriptors: bool = False) -> None:
        path = str(self.plugin_path)
        if self.allow_plugins and path not in sys.path:
            # Append, rather than shadow the host's own Python packages.
            sys.path.append(path)
        importlib.invalidate_caches()
        previous = self.plugins
        self.plugins = {}
        self.collisions = set()
        entries = list(metadata.entry_points(group="nagents.channels"))
        if not self.allow_plugins and not any(Path(item or ".").resolve() == self.plugin_path for item in sys.path):
            # Metadata discovery does not need to make target-directory modules
            # importable in demo, nor execute .pth files or connector descriptors.
            entries.extend(
                entry
                for distribution in metadata.distributions(path=[path])
                for entry in distribution.entry_points
                if entry.group == "nagents.channels"
            )
        for entry in entries:
            if entry.name in self.plugins or entry.name in self.collisions:
                self.plugins.pop(entry.name, None)
                self.collisions.add(entry.name)
                continue
            old = previous.get(entry.name)
            plugin = (
                old
                if old is not None and old.entry.value == entry.value
                else Plugin(entry, entry.name, version=entry.dist.version if entry.dist else "")
            )
            self.plugins[entry.name] = plugin
            if descriptors and self.allow_plugins:
                try:
                    plugin.load()
                except Exception:
                    plugin.description = "Installed descriptor could not be loaded. Check the local installation."
        self.remember_credentials(self.connections)

    def plugin(self, id: str) -> Plugin:
        self.require_live()
        if id in self.collisions:
            raise HTTPException(409, "Multiple installed plugins have this ID. Resolve the installation collision.")
        plugin = self.plugins.get(id)
        if plugin is None:
            raise HTTPException(422, "Channel plugin is not installed. Install it and Refresh first.")
        try:
            plugin.load()
        except Exception:
            raise HTTPException(422, "Channel descriptor could not be loaded. Check the local installation.") from None
        self.remember_credentials(self.connections)
        self.check_public(plugin.public_data())
        return plugin

    def prepare(self, id: str, body: ConnectionInput, main: str) -> Connection:
        self.require_live()
        self.check_revision(body.revision)
        plugin = self.plugin(body.plugin)
        public = dict(body.config)
        old = self.connections.get(id)
        saved = dict(old.secrets) if old is not None and old.plugin == body.plugin else {}
        if old is not None and old.plugin == body.plugin:
            for key in plugin.secret_fields() & old.config.keys():
                saved.setdefault(key, old.config[key])
        private = plugin.secret_fields() | saved.keys() | body.secrets.keys()
        if "name" in public or "name" in private or public.keys() & private:
            raise HTTPException(422, "Instance name is server controlled; write-only fields belong in secrets.")
        for key, value in body.secrets.items():
            if value == "":
                saved.pop(key, None)
            else:
                saved[key] = value
        self.check_public(public, additional=saved)
        self.check_public(plugin.public_data(), additional=saved)
        combined = _object_copy({**public, **saved, "name": id})
        # Name is injected routing metadata, not necessarily a schema property.
        schema_values = {key: value for key, value in combined.items() if key != "name"}
        properties = plugin.schema.get("properties", {})
        if isinstance(properties, dict) and "name" in properties:
            schema_values["name"] = id
        try:
            _validate_schema(schema_values, plugin.schema if body.enabled else {**plugin.schema, "required": []})
        except Exception:
            raise HTTPException(422, "Connection fields do not match the installed plugin schema.") from None
        return Connection(plugin=body.plugin, enabled=body.enabled, config=public, secrets=saved, main_session_id=main)

    def construct(self, id: str, connection: Connection) -> Channel:
        self.require_live()
        # Retain before factory invocation, even if construction/validation later
        # fails: connector code may already have captured these values.
        self.remember_credentials({id: connection})
        self.check_public(self.public_config(connection))
        try:
            plugin = self.plugin(connection.plugin)
            # Descriptor discovery can identify write-only fields in older saved
            # configurations that predate the separate secrets object.
            self.remember_credentials({id: connection})
            self.check_public(self.public_config(connection))
            self.check_public(plugin.public_data())
            channel = plugin.load()(_object_copy({**connection.config, **connection.secrets, "name": id}))
            if not isinstance(channel, Channel) or channel.name != id:
                raise ValueError("Connector did not preserve instance identity")
            self.check_public(json.loads(_bind(channel).catalog))
            return channel
        except Exception:
            raise HTTPException(
                422, "Channel configuration could not be initialized. Check the installed plugin and fields."
            ) from None

    def check_revision(self, revision: str) -> None:
        if revision != self.revision:
            raise HTTPException(409, "Channel configuration changed. Refresh before saving.")

    def require_live(self) -> None:
        if not self.allow_plugins:
            raise HTTPException(
                501,
                "OFFLINE DEMO: connector execution and channel configuration changes are disabled. Restart without --demo.",
            )

    def save(self, connections: dict[str, Connection]) -> None:
        self.require_live()
        self.remember_credentials(self.connections)
        self.remember_credentials(connections)
        for connection in connections.values():
            self.check_public(self.public_config(connection))
        revision = secrets.token_hex(32)
        payload = SavedCatalog(revision=revision, connections=connections).model_dump_json().encode()
        if len(payload) > 1024 * 1024:
            raise HTTPException(422, "Channel configuration exceeds the storage limit.")
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".credentials-")
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name == "posix":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.connections = connections
        self.revision = revision

    def public_config(self, connection: Connection) -> dict[str, JsonValue]:
        plugin = self.plugins.get(connection.plugin)
        private = set(connection.secrets) | (plugin.secret_fields() if plugin else set())
        return {key: value for key, value in connection.config.items() if key not in private}

    def remember_credentials(self, connections: dict[str, Connection]) -> None:
        values: list[object] = []
        for connection in connections.values():
            values.append(connection.secrets)
            plugin = self.plugins.get(connection.plugin)
            if plugin is not None:
                values.append(
                    {key: connection.config[key] for key in plugin.secret_fields() & connection.config.keys()}
                )
        try:
            self.protection.remember(values)
        except CredentialProtectionError:
            raise HTTPException(422, "Channel credential protection capacity or data is invalid.") from None

    def check_public(self, value: object, *, additional: object = None) -> None:
        try:
            self.protection.check(value, additional=additional)
        except CredentialProtectionError:
            raise HTTPException(422, "Channel public data was rejected by credential protection.") from None

    def snapshot(self, bindings: list[dict[str, str]]) -> dict[str, object]:
        self.remember_credentials(self.connections)
        connections: list[dict[str, object]] = []
        for id, connection in self.connections.items():
            plugin = self.plugins.get(connection.plugin)
            private = set(connection.secrets) | (plugin.secret_fields() if plugin else set())
            status, error = self.status.get(id, ("disabled" if not connection.enabled else "unavailable", ""))
            public = self.public_config(connection)
            self.check_public(public)
            connections.append(
                {
                    "id": id,
                    "plugin": connection.plugin,
                    "enabled": connection.enabled,
                    "status": status,
                    "error": error,
                    "config": public,
                    "secret_fields": sorted(private),
                    "configured_secrets": sorted(private & (connection.secrets.keys() | connection.config.keys())),
                    "main_session_id": connection.main_session_id,
                }
            )
        plugins: list[dict[str, object]] = []
        for id, plugin in sorted(self.plugins.items()):
            public_plugin = plugin.public_data()
            self.check_public(public_plugin)
            plugins.append({"id": id, **public_plugin})
        return {
            "revision": self.revision,
            "plugin_path": str(self.plugin_path),
            "plugins": plugins,
            "connections": connections,
            "bindings": bindings,
        }
