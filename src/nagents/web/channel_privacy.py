"""Literal host-credential checks at channel configuration/catalog/result boundaries.

This is not a transcript filter or a Python sandbox. Rejecting a whole unsafe
payload preserves JSON schemas and identifiers instead of rewriting their keys.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from nagents.channels.runtime import _json

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX_CREDENTIALS = 1024
MAX_CREDENTIAL_BYTES = 1024 * 1024
EMBEDDED_LITERAL_MIN = 8


class CredentialProtectionError(ValueError):
    """A fixed, value-free failure; never include the rejected key or value."""


class CredentialGuard:
    def __init__(self) -> None:
        self._literals: frozenset[str] = frozenset()
        self._short: tuple[re.Pattern[str], ...] = ()
        self._long: tuple[str, ...] = ()

    @staticmethod
    def _bounded(value: object) -> None:
        try:
            # Same finite-JSON, 1 MiB, 10,000-node, 32-level bound as core channels.
            _json(value)
        except Exception:
            raise CredentialProtectionError("Channel data exceeds the credential-check policy.") from None

    @classmethod
    def _values(cls, value: object) -> Iterator[str]:
        if isinstance(value, str):
            if value:
                yield value
        elif type(value) in (int, float):
            yield str(value)
        elif isinstance(value, dict):
            # Configuration field names, booleans and null are not credentials.
            for child in value.values():
                yield from cls._values(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._values(child)

    def remember(self, credentials: object) -> None:
        """Retain injected/stored values, including retired values, for this host lifetime."""
        self._bounded(credentials)
        literals = self._literals | frozenset(self._values(credentials))
        if (
            len(literals) > MAX_CREDENTIALS
            or sum(len(value.encode("utf-8")) for value in literals) > MAX_CREDENTIAL_BYTES
        ):
            raise CredentialProtectionError("Channel credential protection capacity exceeded.")
        if literals == self._literals:
            return
        self._literals = literals
        self._long = tuple(value for value in literals if len(value) >= EMBEDDED_LITERAL_MIN)
        self._short = tuple(
            re.compile(r"(?<![\w.-])" + re.escape(value) + r"(?![\w.-])")
            for value in literals
            if len(value) < EMBEDDED_LITERAL_MIN
        )

    def check(self, value: object, *, additional: object = None) -> None:
        """Check bounded connector/public-config JSON without changing any content.

        Short credentials match complete scalar values or delimited text tokens,
        never substrings of identifiers. Keys match only exactly or by a long
        literal. Encoded/fragmented secrets are outside this literal policy.
        """
        if additional is not None:
            candidate = CredentialGuard()
            candidate._literals = self._literals
            candidate._long = self._long
            candidate._short = self._short
            candidate.remember(additional)
            candidate.check(value)
            return
        self._bounded(value)

        def inspect(item: object, *, key: bool = False) -> None:
            if isinstance(item, str):
                if (
                    item in self._literals
                    or any(value in item for value in self._long)
                    or (not key and any(pattern.search(item) for pattern in self._short))
                ):
                    raise CredentialProtectionError("Channel data contains a protected credential.")
            elif type(item) in (int, float):
                if str(item) in self._literals:
                    raise CredentialProtectionError("Channel data contains a protected credential.")
            elif isinstance(item, dict):
                for name, child in item.items():
                    inspect(name, key=True)
                    inspect(child)
            elif isinstance(item, list):
                for child in item:
                    inspect(child)

        inspect(value)
