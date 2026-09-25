"""Detached channel catalog and one-attempt outbound dispatch.

The caller owns connector open/close/listen and authorization. Binding captures
immutable serialized configuration; public catalog/schema results are detached.
"""

from __future__ import annotations

import json
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING
from typing import ParamSpec
from typing import TypeVar
from typing import cast

from .capabilities import content_capabilities_catalog
from .capabilities import validate_content_send
from .types import Channel
from .types import ChannelAction
from .types import ChannelDelivery
from .types import ChannelError
from .types import ChannelFile
from .types import ChannelSend
from .types import ChannelValue

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Iterator

    from nagents.tools.registry import ToolRegistry
    from nagents.types import ToolDefinition

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_TEXT_LENGTH = 256 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
P = ParamSpec("P")
T = TypeVar("T")
_TOOL_NAMES = ("channel_list", "channel_send", "channel_action")

MAX_OUTBOUND_FILES = 3
MAX_OUTBOUND_FILE_BYTES = 20 * 1024 * 1024
MAX_OUTBOUND_TOTAL_BYTES = 30 * 1024 * 1024
_OUTBOUND_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".md": "text/markdown",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".ogg": "audio/ogg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
}


def _outbound_media_type(name: str) -> str:
    return _OUTBOUND_MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _outbound_files(workspace: Path | None, paths: object) -> tuple[ChannelFile, ...]:
    """Read bounded workspace files the model asked to attach, or fail closed."""
    if paths is None:
        return ()
    if not isinstance(paths, (list, tuple)) or not all(isinstance(item, str) for item in paths):
        raise ChannelError("attachments must be a list of workspace-relative paths")
    if not paths:
        return ()
    if len(paths) > MAX_OUTBOUND_FILES:
        raise ChannelError(f"At most {MAX_OUTBOUND_FILES} attachments are allowed per message")
    if workspace is None:
        raise ChannelError("Outbound attachments require a workspace")
    root = workspace.resolve()
    files: list[ChannelFile] = []
    total = 0
    for item in paths:
        relative = Path(_text(item, "attachment path", limit=4096))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ChannelError("Attachment paths must be workspace-relative")
        probe = root
        for part in relative.parts:
            probe = probe / part
            if probe.is_symlink():
                raise ChannelError("Attachment paths may not traverse symlinks")
        if not probe.resolve().is_relative_to(root):
            raise ChannelError("Attachment path is outside the workspace")
        if not probe.is_file():
            raise ChannelError("Attachment is not a regular file")
        size = probe.stat().st_size
        if size > MAX_OUTBOUND_FILE_BYTES:
            raise ChannelError("Attachment exceeds the per-file size limit")
        if total + size > MAX_OUTBOUND_TOTAL_BYTES:
            raise ChannelError("Attachments exceed the total size limit")
        # Stat is only a preflight: the file can grow before/during the read.
        limit = min(MAX_OUTBOUND_FILE_BYTES, MAX_OUTBOUND_TOTAL_BYTES - total)
        with probe.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > MAX_OUTBOUND_FILE_BYTES:
            raise ChannelError("Attachment exceeds the per-file size limit")
        total += len(data)
        if total > MAX_OUTBOUND_TOTAL_BYTES:
            raise ChannelError("Attachments exceed the total size limit")
        files.append(ChannelFile(filename=probe.name, media_type=_outbound_media_type(probe.name), data=data))
    return tuple(files)


def _text(value: object, field: str, *, blank: bool = False, limit: int = 512) -> str:
    if not isinstance(value, str) or len(value) > limit or (not blank and not value.strip()) or "\x00" in value:
        raise ChannelError(f"Invalid {field}")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ChannelError(f"Invalid {field}") from None
    return value


def _identifier(value: object, field: str) -> str:
    value = _text(value, field, limit=64)
    if not _IDENTIFIER.fullmatch(value):
        raise ChannelError(f"Invalid {field}; use a stable configuration identifier")
    return value


def _json(value: object) -> str:
    """Bounded strict JSON, rejecting non-string keys, cycles, NaN and Infinity."""
    nodes = 0
    characters = 0

    def check(item: object, depth: int) -> None:
        nonlocal nodes, characters
        nodes += 1
        if depth > 32 or nodes > 10000:
            raise ChannelError("Channel JSON is too complex")
        if type(item) is str:
            characters += len(item)
            if characters > MAX_PAYLOAD_BYTES:
                raise ChannelError("Channel JSON exceeds the payload limit")
            return
        if item is None or type(item) in (int, bool):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(isinstance(key, str) for key in item):
            for key in item:
                check(key, depth + 1)
            for child in item.values():
                check(child, depth + 1)
            return
        raise ChannelError("Channel payload must contain finite JSON values and string keys")

    check(value, 0)
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(result.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ChannelError("Invalid channel JSON") from None
    if size > MAX_PAYLOAD_BYTES:
        raise ChannelError("Channel JSON exceeds the payload limit")
    return result


def _object_copy(value: object) -> dict[str, ChannelValue]:
    if not isinstance(value, dict):
        raise ChannelError("Channel arguments and metadata must be JSON objects")
    return cast("dict[str, ChannelValue]", json.loads(_json(value)))


def _validate_schema(value: ChannelValue, schema: dict[str, ChannelValue]) -> None:
    """Validate the contract's JSON Schema subset; connectors validate operations."""
    expected = schema.get("type")
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if isinstance(expected, str) and not matches.get(expected, False):
        raise ChannelError("Action arguments do not match the advertised schema")
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        raise ChannelError("Action argument is not an advertised enum value")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ChannelError("Invalid action schema")
        if any(not isinstance(key, str) or key not in value for key in required):
            raise ChannelError("Missing required action argument")
        for key, child in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                _validate_schema(child, child_schema)
            elif schema.get("additionalProperties", True) is False:
                raise ChannelError("Unknown action argument")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for child in value:
            _validate_schema(child, cast("dict[str, ChannelValue]", schema["items"]))


@dataclass(frozen=True)
class _Binding:
    channel: Channel
    name: str
    catalog: str
    actions: tuple[tuple[str, str], ...]
    content: str


def _bind(channel: Channel) -> _Binding:
    if not isinstance(channel, Channel):
        raise ChannelError("Channels must implement Channel")
    name = _identifier(channel.name, "channel name")
    description = _text(channel.description, "channel description", blank=True, limit=8192)
    capabilities = tuple(channel.capabilities)
    for capability in capabilities:
        _identifier(capability, "channel capability")
    actions: list[ChannelValue] = []
    schemas: dict[str, str] = {}
    for action in tuple(channel.actions):
        if not isinstance(action, ChannelAction):
            raise ChannelError("Invalid channel action")
        action_name = _identifier(action.name, "action name")
        if action_name in schemas:
            raise ChannelError("Duplicate channel action name")
        parameters = _object_copy(action.parameters)
        if parameters.get("type") != "object":
            raise ChannelError("Action parameters must advertise an object schema")
        schemas[action_name] = _json(parameters)
        actions.append(
            {
                "name": action_name,
                "description": _text(action.description, "action description", blank=True, limit=8192),
                "parameters": parameters,
            }
        )
    entry: dict[str, ChannelValue] = {
        "name": name,
        "description": description,
        "capabilities": list(capabilities),
        "actions": actions,
    }
    content = ""
    if channel.content_capabilities is not None:
        descriptor = content_capabilities_catalog(channel.content_capabilities)
        entry["content_capabilities"] = descriptor
        content = _json(descriptor)
    return _Binding(channel, name, _json(entry), tuple(schemas.items()), content)


def _failure(error: Exception, operation: str, *, side_effect: bool = False) -> ChannelError:
    """Expose sanitized errors and flags in str(error), which ToolExecutor retains."""
    if isinstance(error, ChannelError):
        retry_after = error.retry_after
        if type(retry_after) not in (int, float) or not math.isfinite(retry_after) or retry_after < 0:
            retry_after = 0
        message = str(error)[:4096]
        outcome_unknown = bool(error.outcome_unknown)
    else:
        message = f"Channel {operation} failed"
        retry_after = 0
        outcome_unknown = side_effect
    details = {"error": message, "retry_after": retry_after, "outcome_unknown": outcome_unknown}
    return ChannelError(_json(details), retry_after=retry_after, outcome_unknown=outcome_unknown)


def _tool_arguments(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Normalize argument-binding errors while retaining the tool's schema/signature."""

    @wraps(func)
    async def invoke(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            operation = func(*args, **kwargs)
        except TypeError:
            raise _failure(ChannelError("Invalid channel tool arguments"), "tool") from None
        return await operation

    return invoke


class ChannelDispatcher:
    """A bound catalog and tools, independent of execution and connector lifecycle."""

    def __init__(self, channels: tuple[Channel, ...] = (), *, workspace: Path | None = None) -> None:
        self.workspace = workspace.resolve() if workspace is not None else None
        self._bindings = tuple(_bind(channel) for channel in channels)
        self._routes = {binding.name: binding for binding in self._bindings}
        if len(self._routes) != len(self._bindings):
            raise ChannelError("Channel names must be unique")

    def catalog(self) -> list[dict[str, ChannelValue]]:
        """Return a fresh, detached snapshot, including for an empty dispatcher."""
        return [cast("dict[str, ChannelValue]", json.loads(binding.catalog)) for binding in self._bindings]

    def contains_channel(self, name: str, channel: Channel) -> bool:
        """Check bound name AND connector identity without trusting mutable names."""
        binding = self._routes.get(name)
        return binding is not None and binding.channel is channel

    def action_schema(self, channel: str, action: str) -> dict[str, ChannelValue]:
        """Return a detached advertised schema; reject unknown channels/actions."""
        binding = self._route(channel)
        _identifier(action, "action")
        schema = dict(binding.actions).get(action)
        if schema is None:
            raise ChannelError("Unknown channel action")
        return cast("dict[str, ChannelValue]", json.loads(schema))

    def _route(self, channel: str) -> _Binding:
        _identifier(channel, "channel")
        binding = self._routes.get(channel)
        if binding is None:
            raise ChannelError("Unknown channel")
        return binding

    @contextmanager
    def register_tools(self, registry: ToolRegistry) -> Iterator[tuple[ToolDefinition, ...]]:
        """Own temporary tool registration without replacing or restoring tools.

        Preflight all names before registration. On every exit remove only the
        exact definitions installed here, preserving any subsequent replacement.
        Connector lifecycle and execution policy remain the caller's concern.
        """
        if any(registry.get(name) is not None for name in _TOOL_NAMES):
            raise ChannelError("Channel tool name collision")
        owned: list[ToolDefinition] = []
        try:
            owned.append(registry.register(self.channel_list, name="channel_list"))
            owned.append(registry.register(self.channel_send, name="channel_send"))
            owned.append(
                registry.register(
                    self.channel_action,
                    name="channel_action",
                    parameters={
                        "type": "object",
                        "properties": {
                            "channel": {"type": "string"},
                            "action": {"type": "string"},
                            "arguments": {"type": "object"},
                        },
                        "required": ["channel", "action", "arguments"],
                        "additionalProperties": False,
                    },
                )
            )
            yield tuple(owned)
        finally:
            for tool in owned:
                if registry.get(tool.name) is tool:
                    registry.unregister(tool.name)

    @_tool_arguments
    async def channel_list(self) -> list[dict[str, ChannelValue]]:
        """Discover configured channels, descriptive capabilities, and action schemas."""
        try:
            return self.catalog()
        except Exception as error:
            raise _failure(error, "list") from None

    @_tool_arguments
    async def channel_send(
        self,
        channel: str,
        destination: str,
        text: str,
        thread_id: str = "",
        reply_to: str = "",
        attachments: list[str] | None = None,
    ) -> dict[str, ChannelValue]:
        """Send one explicit message to a chosen channel and destination, without retries.

        Args:
            attachments: Up to three workspace-relative file paths to upload with the message. Connectors may reject unsupported attachments; the text may be empty when files are present.
            reply_to: Remote transport message ID in the chosen channel/conversation, not an ingress message_id. Use the inbound reply_to when replying to that event; otherwise leave empty.
        """
        try:
            binding = self._route(channel)
            body = _text(text, "text", blank=True, limit=MAX_TEXT_LENGTH)
            files = _outbound_files(self.workspace, attachments)
            if not body and not files:
                raise ChannelError("A channel message needs text or an attachment")
            message = ChannelSend(
                destination=_text(destination, "destination"),
                text=body,
                thread_id=_text(thread_id, "thread_id", blank=True),
                reply_to=_text(reply_to, "reply_to", blank=True),
                files=files,
            )
            if binding.content:
                validate_content_send(_object_copy(json.loads(binding.content)), message)
        except Exception as error:
            raise _failure(error, "send") from None
        try:
            delivery = await binding.channel.send(message)
        except Exception as error:
            raise _failure(error, "send", side_effect=True) from None
        try:
            if not isinstance(delivery, ChannelDelivery) or not isinstance(delivery.message_ids, tuple):
                raise TypeError("Invalid delivery")
            ids = [_text(value, "delivery message ID") for value in delivery.message_ids]
            return _object_copy({"message_ids": ids, "metadata": delivery.metadata})
        except Exception:
            raise _failure(ChannelError("Invalid channel delivery response", outcome_unknown=True), "send") from None

    @_tool_arguments
    async def channel_action(
        self, channel: str, action: str, arguments: dict[str, ChannelValue]
    ) -> dict[str, ChannelValue]:
        """Perform one advertised connector action; consult channel_list for its argument schema."""
        try:
            binding = self._route(channel)
            schema = self.action_schema(channel, action)
            copied = _object_copy(arguments)
            _validate_schema(copied, schema)
        except Exception as error:
            raise _failure(error, "action") from None
        try:
            result = await binding.channel.action(action, copied)
        except Exception as error:
            raise _failure(error, "action", side_effect=True) from None
        try:
            return _object_copy(result)
        except Exception:
            raise _failure(ChannelError("Invalid channel action response", outcome_unknown=True), "action") from None
