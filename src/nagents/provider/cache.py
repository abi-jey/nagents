"""Bounded conversation cache telemetry, not a server-side cache controller.

Only route/identity metadata and provider-reported token counters are retained.
No prompts, tools, credentials, response bodies, or executable callables belong
here. Missing usage is not an estimated cache miss. Eviction/restarts discard
telemetry, not conversation history or upstream cache state.
"""

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

from ..events import Usage


@dataclass(frozen=True)
class ConversationCacheKey:
    endpoint: str
    model: str
    conversation_id: str


@dataclass(frozen=True)
class ConversationCacheSnapshot:
    key: ConversationCacheKey
    request_count: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    input_reported_requests: int = 0
    last_input_tokens: int = 0
    last_cached_tokens: int = 0
    last_input_reported: bool = False


class ConversationCacheTracker:
    """Provider-local, thread-safe LRU of completed request usage.

    A conversation identity must be supplied explicitly (normally via the
    provider's session_context). Unscoped calls are deliberately not combined
    into an anonymous conversation. Keys and snapshots are immutable; reads do
    not refresh LRU ordering. Record exactly once after protocol validation,
    not once per text/tool event carrying the same usage.
    """

    def __init__(self, max_entries: int = 128) -> None:
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self._max_entries = max_entries
        self._entries: OrderedDict[ConversationCacheKey, ConversationCacheSnapshot] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def key(endpoint: str, model: str, conversation_id: str) -> ConversationCacheKey:
        """Share route identity without retaining URL userinfo, query or fragment."""
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        route = urlunsplit((parts.scheme, host, parts.path, "", ""))
        return ConversationCacheKey(route, model, conversation_id)

    def record(self, key: ConversationCacheKey, usage: Usage) -> ConversationCacheSnapshot | None:
        """Count one successful request, including requests with missing usage."""
        # Bound metadata size as well as entry count for long-lived providers.
        if not key.conversation_id or any(
            len(value) > 4096 for value in (key.endpoint, key.model, key.conversation_id)
        ):
            return None
        key = self.key(key.endpoint, key.model, key.conversation_id)
        with self._lock:
            previous = self._entries.pop(key, ConversationCacheSnapshot(key))
            snapshot = ConversationCacheSnapshot(
                key=key,
                request_count=previous.request_count + 1,
                input_tokens=previous.input_tokens + (usage.prompt_tokens if usage.prompt_reported else 0),
                cached_tokens=previous.cached_tokens + usage.cached_tokens,
                input_reported_requests=previous.input_reported_requests + int(usage.prompt_reported),
                last_input_tokens=usage.prompt_tokens if usage.prompt_reported else 0,
                last_cached_tokens=usage.cached_tokens,
                last_input_reported=usage.prompt_reported,
            )
            self._entries[key] = snapshot
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return snapshot

    def snapshot(self) -> tuple[ConversationCacheSnapshot, ...]:
        """Return detached immutable snapshots, oldest to most recently recorded."""
        with self._lock:
            return tuple(self._entries.values())

    def get(self, endpoint: str, model: str, conversation_id: str) -> ConversationCacheSnapshot | None:
        with self._lock:
            return self._entries.get(self.key(endpoint, model, conversation_id))
