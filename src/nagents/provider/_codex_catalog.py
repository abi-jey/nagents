"""Fresh stable Codex catalog compatibility, without sharing provider credentials.

The public package metadata contains a version, never a model allowlist. Every
explicit catalog lookup revalidates it; only overlapping lookups and temporary
metadata outages reuse the process-local last known version.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
from typing import TYPE_CHECKING
from weakref import ReferenceType
from weakref import WeakKeyDictionary
from weakref import ref

import aiohttp

from ..adapters._validation import load_object

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)
_catalog_logger: ContextVar[logging.Logger] = ContextVar("codex_catalog_logger", default=logger)

METADATA_URL = "https://registry.npmjs.org/@openai/codex/latest"
FALLBACK_VERSION = "0.162.0"
METADATA_TIMEOUT = 2.0
FAILURE_BACKOFF = 300.0
MAX_METADATA_BYTES = 64 * 1024
_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\Z")
_ETAG = re.compile(r"[\x21-\x7e]{1,1024}\Z")


@contextmanager
def catalog_logging(target: logging.Logger) -> Iterator[None]:
    """Route only safe catalog diagnostics through the current host request."""
    token = _catalog_logger.set(target)
    try:
        yield
    finally:
        _catalog_logger.reset(token)


def _version(value: object) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value) or value == "0.0.0":
        raise ValueError("Invalid stable Codex version metadata")
    return value


def _precedence(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


class CatalogVersion:
    """A bounded metadata cache, independent of accounts and event-loop lifetime."""

    def __init__(self) -> None:
        self.version = FALLBACK_VERSION
        self.source = "bundled"
        self.etag = ""
        self.retry_at = 0.0
        self.revision = 0
        self._locks: WeakKeyDictionary[asyncio.AbstractEventLoop, ReferenceType[asyncio.Lock]] = WeakKeyDictionary()

    def _result(self, status: str) -> str:
        _catalog_logger.get().info(
            "Codex catalog compatibility: version=%s source=%s status=%s", self.version, self.source, status
        )
        return self.version

    async def resolve(self, timeout: float = METADATA_TIMEOUT) -> str:
        if monotonic() < self.retry_at:
            return self._result("backoff")
        revision = self.revision
        loop = asyncio.get_running_loop()
        previous = self._locks.get(loop)
        lock = previous() if previous else None
        if lock is None:
            lock = asyncio.Lock()
            # A contended asyncio.Lock retains its loop. Both references must
            # be weak so short-lived client loops cannot accumulate here.
            self._locks[loop] = ref(lock)
        owns_refresh = False
        try:
            async with asyncio.timeout(min(timeout, METADATA_TIMEOUT)):
                async with lock:
                    # Another overlapping request already checked the upstream.
                    if revision != self.revision:
                        return self._result("coalesced")
                    owns_refresh = True
                    version, etag = await self._fetch()
                    if _precedence(version) < _precedence(self.version):
                        raise ValueError("Codex version metadata moved backwards")
                    self.version, self.etag, self.source = version, etag, "npm"
                    self.retry_at = 0.0
                    self.revision += 1
                    return self._result("checked")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            if owns_refresh:
                self.retry_at = monotonic() + FAILURE_BACKOFF
                self.revision += 1
            # No upstream exception/body/header can enter application logs.
            _catalog_logger.get().warning(
                "Codex version metadata unavailable; retaining compatibility version %s", self.version
            )
            return self._result("fallback")

    async def _fetch(self) -> tuple[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "ngn-catalog-version"}
        if self.etag:
            headers["If-None-Match"] = self.etag
        # This session is never supplied an OAuth token, account ID, API key,
        # provider headers, cookies, proxy/netrc settings, or tracing hooks.
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=METADATA_TIMEOUT),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            ) as client,
            client.get(METADATA_URL, headers=headers, allow_redirects=False) as response,
        ):
            if response.status == 304 and self.etag:
                return self.version, self.etag
            if response.status != 200:
                raise ValueError("Codex version metadata request failed")
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > MAX_METADATA_BYTES:
                    raise ValueError("Codex version metadata exceeded its size limit")
            document = load_object(body.decode("utf-8"))
            if document.get("name") != "@openai/codex":
                raise ValueError("Unexpected Codex package metadata")
            version = _version(document.get("version"))
            etag = response.headers.get("ETag", "")
            return version, etag if _ETAG.fullmatch(etag) else ""


_versions = CatalogVersion()


async def catalog_version(timeout: float) -> str:
    return await _versions.resolve(timeout)
