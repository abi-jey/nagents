"""Transport-neutral contracts for independently installable channel connectors."""

import json
import math
import re
from abc import ABC
from abc import abstractmethod
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from itertools import islice
from typing import Literal
from typing import TypeAlias
from urllib.parse import unquote_plus

from nagents.events import Event
from nagents.types import JsonSchema

ChannelValue: TypeAlias = str | int | float | bool | list["ChannelValue"] | dict[str, "ChannelValue"] | None
ChannelExecutionPhase: TypeAlias = Literal[
    "run_started", "tool_requested", "tool_completed", "waiting_for_approval", "completed", "failed", "cancelled"
]

_ARGUMENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_CREDENTIAL_NAME = (
    r"auth(?:orization)?|cookies?|credentials?|secrets?|tokens?|passwords?|passwd|passphrase|"
    r"api[_.-]?key|access[_.-]?key|private[_.-]?key|signing[_.-]?key|session[_.-]?(?:key|id)|pwd|signature"
)
_PRIVATE_ARGUMENT = re.compile(
    _CREDENTIAL_NAME + r"|text|content|body|message|prompt|reasoning|thinking|system|developer|result|output|"
    r"headers|environment|(?:^|[_.-])(?:env|key|pass|pin|otp|sig)(?:$|[_.-])",
    re.IGNORECASE,
)
_CREDENTIAL_VALUE = re.compile(
    # Authentication schemes, including embedded header/command values.
    r"\b(?:bearer|basic|digest)\s+\S+|"
    # URL authority userinfo, plus scheme-less user:password@host forms.
    r"//[^/?#\s]*@|(?<![\w])[^:/@\s]+:[^/@\s]+@|"
    # JWT (including unsigned tokens) and common provider/service token prefixes.
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*|"
    r"(?<![A-Za-z0-9])(?:sk-|rk-|gh[pousr]_|github_pat_|glpat-|xox[baprs]-|xapp-|hf_|npm_|pypi-|whsec_)"
    r"[A-Za-z0-9_-]{6,}|"
    r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9_]{6,}|"
    r"\bAIza[A-Za-z0-9_-]{20,}|\b(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"\bSG\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|\bya29\.[A-Za-z0-9_-]{8,}|"
    r"\b\d{6,12}:[A-Za-z0-9_-]{20,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|"
    # Assignment names can have prefixes/suffixes: client_secret, AWSAccessKeyId,
    # X-Amz-Signature, JSON fields, environment variables and URL query parameters.
    r"(?<![\w])[\w.-]*(?:" + _CREDENTIAL_NAME + r")[\w.-]*['\"]?\s*[:=]|"
    r"\b(?:key|pass|sig)['\"]?\s*[:=]|"
    # Also cover space-separated credential flags and curl's short user option.
    r"--?[\w.-]*(?:" + _CREDENTIAL_NAME + r"|user|key|pass|sig)[\w.-]*(?:\s*=\s*|\s+)\S+|"
    r"\bcurl\b.*\s-u\s*\S+|\bsshpass\b",
    re.IGNORECASE,
)


def _display_argument_string(value: str) -> str:
    # Omit long values outright: no prefix is ever taken, scanned or displayed.
    # This keeps work bounded even for huge strings. Every candidate that could
    # be displayed is checked in full, including two percent-decoding passes.
    if len(value) > 80:
        return "[redacted]"
    candidate = value
    for _ in range(3):
        if not candidate.isprintable() or _CREDENTIAL_VALUE.search(candidate):
            return "[redacted]"
        decoded = unquote_plus(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    return value


def sanitize_channel_tool_arguments(arguments: object) -> dict[str, ChannelValue]:
    """Return a detached, bounded JSON summary for conservative display filtering.

    Ordinary strings of at most 80 printable, single-line characters survive.
    Sensitive-key subtrees, common credential patterns, controls and long values
    are redacted in full, never prefix-truncated. Short identifier keys, finite
    numbers, booleans and null may remain. Unknown objects are not stringified.
    Work is bounded to depth 4, 64 nodes and 16 input entries per container, with
    a final 4096-byte JSON limit. This is display data, not executable arguments.

    This cannot detect arbitrary secrets under misleading keys. Hosts must check
    full original arguments against their saved credentials before construction;
    connectors must also check their own credentials before rendering notices.
    """
    nodes = 0

    def summarize(value: object, depth: int) -> ChannelValue:
        nonlocal nodes
        nodes += 1
        if depth > 4 or nodes > 64:
            return "[truncated]"
        if value is None or type(value) is bool:
            return value
        if type(value) is int and -(2**63) <= value < 2**63:
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if type(value) is str:
            return _display_argument_string(value)
        if type(value) is list:
            items: list[ChannelValue] = []
            for child in islice(value, 16):
                if nodes >= 64:
                    break
                items.append(summarize(child, depth + 1))
            if len(value) > len(items):
                items.append("[truncated]")
            return items
        if type(value) is dict:
            result: dict[str, ChannelValue] = {}
            for key, child in islice(value.items(), 16):
                if nodes >= 64:
                    result["[truncated]"] = True
                    break
                if (
                    not isinstance(key, str)
                    or not (_ARGUMENT_KEY.fullmatch(key) or key in ("[redacted key]", "[truncated]"))
                    or _display_argument_string(key) != key
                ):
                    nodes += 1
                    result["[redacted key]"] = "[redacted]"
                else:
                    result[key] = summarize("[redacted]" if _PRIVATE_ARGUMENT.search(key) else child, depth + 1)
            if len(value) > 16:
                result["[truncated]"] = True
            return result
        return "[redacted]"

    result = summarize(arguments, 0)
    if not isinstance(result, dict):
        return {}
    if len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode("utf-8")) > 4096:
        return {"[truncated]": True}
    return result


@dataclass(frozen=True)
class ChannelAttachment:
    """A connector-owned reference, not automatically downloaded or executed."""

    reference: str
    media_type: str = "application/octet-stream"
    filename: str = ""
    size: int = 0


@dataclass(frozen=True)
class ChannelMessage:
    """An external event. Its identity is stable within the connector instance.

    Conversation/thread IDs describe the source; they do not select an Agent
    session. All connectors attached to one listener share that listener's session.
    ``reply_to`` is a transport message ID suitable for replying to this event;
    it can differ from the ingress ``message_id`` used for deduplication.
    Metadata must be JSON-compatible and must not contain credentials.
    """

    message_id: str
    conversation_id: str
    sender_id: str
    text: str = ""
    thread_id: str = ""
    reply_to: str = ""
    event_type: str = "message"
    attachments: tuple[ChannelAttachment, ...] = ()
    metadata: dict[str, ChannelValue] = field(default_factory=dict)
    # Optional generic presentation fields; untrusted transport data, never authority.
    sent_at: float = 0.0
    sender_name: str = ""
    sender_username: str = ""
    conversation_type: str = ""


@dataclass(frozen=True)
class ChannelSend:
    """An explicit outgoing message selected by the application or model."""

    destination: str
    text: str
    thread_id: str = ""
    reply_to: str = ""
    attachments: tuple[ChannelAttachment, ...] = ()
    metadata: dict[str, ChannelValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelDelivery:
    """Remote message identifiers returned after confirmed delivery."""

    message_ids: tuple[str, ...]
    metadata: dict[str, ChannelValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelAction:
    """Advertise an integration-specific operation, such as editing a message."""

    name: str
    description: str
    parameters: JsonSchema


@dataclass(frozen=True)
class ChannelCommand:
    """A connector-recognized host command; the host decides which commands exist."""

    name: str
    arguments: str = ""


@dataclass(frozen=True)
class ChannelActivity:
    """Session activity for transport indicators, independent of model replies."""

    conversation_id: str
    active: bool
    thread_id: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class ChannelExecutionEvent:
    """Host-neutral, source-scoped execution notice, separate from local ChannelEvent.

    The host supplies the owning conversation/thread and correlation IDs, never
    infers a route from tool arguments. Empty optional IDs mean unavailable.
    ``tool_requested`` does not establish execution or approval; ``tool_completed``
    means a result was observed, with ``tool_failed`` indicating an error result.
    Hosts with an approval gate may emit ``waiting_for_approval`` themselves.

    No raw Event, assistant text, reasoning, prompts, tool results or exception
    text fields are carried. Arguments are display-filtered on construction and
    again by the dispatch helper; this is not guaranteed detection of arbitrary
    secrets. Hosts check original arguments against saved credentials first;
    connectors check their own credentials and escape untrusted display data.
    """

    conversation_id: str
    session_id: str
    phase: ChannelExecutionPhase
    thread_id: str = ""
    run_id: str = ""
    activation_id: str = ""
    call_id: str = ""
    tool_name: str = ""
    tool_arguments: dict[str, ChannelValue] = field(default_factory=dict)
    tool_failed: bool = False
    message_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_arguments", sanitize_channel_tool_arguments(self.tool_arguments))

    @property
    def destination(self) -> str:
        """The notice's owning conversation, for ChannelSend-compatible routing."""
        return self.conversation_id


class ChannelError(Exception):
    """A sanitized connector failure suitable for a tool result.

    No implicit send retries are performed. ``outcome_unknown`` means a remote
    side effect may have happened. Never put credential-bearing URLs in errors.
    """

    def __init__(self, message: str, *, retry_after: float = 0, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.outcome_unknown = outcome_unknown


# Return only after durable inbox admission. Raising leaves the source event
# unacknowledged. Acceptance does not mean model execution or delivery succeeded.
ChannelReceiver: TypeAlias = Callable[[ChannelMessage], Awaitable[None]]


@dataclass(frozen=True)
class ChannelEvent:
    """Observe execution locally; observation never sends a channel reply."""

    session_id: str
    channel: str
    message_id: str
    event: Event


ChannelEventHandler: TypeAlias = Callable[[ChannelEvent], Awaitable[None]]


async def discard_event(event: ChannelEvent) -> None:
    """Default local observer for applications interested only in channel actions."""


class Channel(ABC):
    """A trusted connector. Installations remain explicit; imports do not connect.

    ``open`` initializes resources, ``listen`` delivers events with backpressure,
    and ``close`` releases resources after producers/execution have stopped.
    Transport acknowledgements belong to the connector. Agent responses are
    emitted only through explicit send/action tools, never auto-forwarded.
    ``actions`` supplies connector operations through the existing channel_list,
    channel_send and channel_action tools. ``on_event`` is optional execution
    observation for status notices, independent of tools and typing activity.
    """

    name: str
    description: str = ""
    capabilities: tuple[str, ...] = ("receive", "send_text")
    actions: tuple[ChannelAction, ...] = ()

    async def open(self) -> None:
        return None

    @abstractmethod
    async def listen(self, receive: ChannelReceiver) -> None:
        """Deliver events until cancelled, or return when a finite source ends."""

    @abstractmethod
    async def send(self, message: ChannelSend) -> ChannelDelivery:
        """Send once; return confirmed IDs or raise a sanitized ChannelError."""

    async def action(self, name: str, arguments: dict[str, ChannelValue]) -> dict[str, ChannelValue]:
        raise ChannelError(f"Unsupported channel action: {name}")

    async def fetch_attachment(self, attachment: ChannelAttachment) -> tuple[bytes, str]:
        """Download a referenced attachment for model input.

        Only connectors that advertise ``fetch_attachment`` in ``capabilities``
        need to implement this. Return the raw bytes and the effective media type
        within the host's caps; raise a sanitized ``ChannelError`` otherwise. The
        host never treats the returned content as instructions.
        """
        raise ChannelError("This connector does not support attachment downloads")

    def command(self, message: ChannelMessage) -> ChannelCommand | None:
        """Recognize an explicit transport command without doing I/O."""
        return None

    async def activity(self, event: ChannelActivity) -> None:
        """Start/stop an optional typing indicator; close() must stop keepalives."""
        return None

    async def on_event(self, event: ChannelExecutionEvent) -> None:
        """Optionally render a source-scoped status notice; default does nothing.

        Cooperate with cancellation and bound transport I/O. Hosts should await
        dispatch_channel_execution_event for failure isolation and joined,
        bounded dispatch. Never retry an uncertain notice send automatically.
        """
        return None

    async def close(self) -> None:
        return None


ChannelFactory: TypeAlias = Callable[[dict[str, ChannelValue]], Channel]


@dataclass(frozen=True)
class ChannelPlugin:
    """Callable entry point with a configuration schema for management clients.

    JSON Schema properties marked ``writeOnly: true`` are credentials: clients
    collect them separately and never echo saved values. The host validates and
    persists configuration; the factory performs connector-specific validation.
    Ordinary callable factories remain supported by ``load_channel``.
    """

    name: str
    description: str
    config_schema: dict[str, ChannelValue]
    factory: ChannelFactory

    def __call__(self, config: dict[str, ChannelValue]) -> Channel:
        return self.factory(config)
