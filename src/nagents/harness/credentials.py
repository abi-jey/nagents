"""One active provider API login beside ngn's ChatGPT device credentials.

The selection is secret-free routing metadata; the API key is stored in the
same protected 0600 file and is never returned by ``selection()``. Callers that
only need to know where traffic should go use ``selection()``; the provider
reads ``key_for(provider)`` lazily at request time. Only one login is active:
signing in to another provider replaces the previous selection and key.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING

from .private_store import ProtectedFileStore
from .private_store import ProtectedStoreError

if TYPE_CHECKING:
    from pathlib import Path

_API_NAMES = ("auto", "chat_completions", "responses", "messages", "completions")
_AUTH_NAMES = ("auto", "api-key", "chatgpt")
_FIELDS = {"version", "provider", "model", "base_url", "api", "auth", "api_key_env", "api_key"}
_UNREADABLE = "Saved provider login is unreadable; run ngn login again."
_UNSAFE_KEY = "Provider login contains an unusable API key; run ngn login again."


class ProviderLoginError(ProtectedStoreError):
    """Safe, human-readable provider-login errors without secret values."""


@dataclass(frozen=True)
class ProviderLogin:
    """Active provider routing plus an optional write-only API key."""

    provider: str
    model: str = ""
    base_url: str = ""
    api: str = "auto"
    auth: str = "api-key"
    api_key_env: str = ""
    api_key: str = field(default="", repr=False, compare=False)


def _text(value: object, *, limit: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
    ):
        raise ProviderLoginError(_UNREADABLE)
    return value


class ProviderLoginStore(ProtectedFileStore):
    """Serialization and validation for ``login.json`` below the private store."""

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(
            path,
            filename="login.json",
            subject="Provider",
            error=ProviderLoginError,
            malformed=_UNREADABLE,
        )

    def _parse(self) -> ProviderLogin | None:
        try:
            with self._directory() as directory:
                raw = self._read_bytes(directory)
            if raw is None:
                return None
            data: object = json.loads(raw)
            if (
                not isinstance(data, dict)
                or set(data) != _FIELDS
                or type(data["version"]) is not int
                or data["version"] != 1
            ):
                raise ProviderLoginError(_UNREADABLE)
            provider = _text(data["provider"], limit=64)
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", provider) is None:
                raise ProviderLoginError(_UNREADABLE)
            model = _text(data["model"])
            base_url = _text(data["base_url"])
            api = _text(data["api"], limit=32)
            auth = _text(data["auth"], limit=32)
            api_key_env = _text(data["api_key_env"], limit=256)
            api_key = data["api_key"]
            if api not in {*_API_NAMES, ""} or auth not in _AUTH_NAMES:
                raise ProviderLoginError(_UNREADABLE)
            if api_key_env and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env) is None:
                raise ProviderLoginError(_UNREADABLE)
            if not isinstance(api_key, str) or (api_key and re.fullmatch(r"[\x21-\x7e]{1,8192}", api_key) is None):
                raise ProviderLoginError(_UNSAFE_KEY)
            return ProviderLogin(provider, model, base_url, api, auth, api_key_env, api_key)
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderLoginError(_UNREADABLE) from None
        except OSError:
            raise ProviderLoginError(self._unsafe) from None

    def selection(self) -> ProviderLogin | None:
        """Secret-free active selection, or None when absent or unreadable."""
        try:
            login = self._parse()
        except ProviderLoginError:
            return None
        return None if login is None else replace(login, api_key="")

    def key_for(self, provider: str) -> str:
        """Stored key for the active provider, or an empty string."""
        try:
            login = self._parse()
        except ProviderLoginError:
            return ""
        if login is None or login.provider != provider:
            return ""
        return login.api_key

    def save(self, login: ProviderLogin) -> None:
        if not isinstance(login, ProviderLogin) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", login.provider):
            raise ProviderLoginError("Provider login names must be 1..64 letters, digits, '_' or '-'.")
        for value in (login.model, login.base_url, login.api, login.auth, login.api_key_env):
            if not isinstance(value, str) or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
                raise ProviderLoginError(_UNREADABLE)
        if login.api not in _API_NAMES or login.auth not in _AUTH_NAMES:
            raise ProviderLoginError(_UNREADABLE)
        if login.api_key_env and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", login.api_key_env) is None:
            raise ProviderLoginError("api_key_env must be an environment variable name, never a literal secret.")
        if login.api_key and re.fullmatch(r"[\x21-\x7e]{1,8192}", login.api_key) is None:
            raise ProviderLoginError(_UNSAFE_KEY)
        payload = {
            "version": 1,
            "provider": login.provider,
            "model": login.model,
            "base_url": login.base_url,
            "api": login.api,
            "auth": login.auth,
            "api_key_env": login.api_key_env,
            "api_key": login.api_key,
        }
        encoded = json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._write_bytes(encoded, prefix=".provider-login-")

    def remove(self) -> None:
        """Delete any saved provider login and key, tolerating an absent store."""
        self._unlink()

    def status(self) -> str:
        try:
            login = self._parse()
        except ProviderLoginError:
            return "Saved provider login is unusable; run ngn login again."
        if login is None:
            return "Not signed in"
        model = login.model or "provider default model"
        if login.api_key:
            where = "API key stored in ngn's private credential store"
        elif login.api_key_env:
            where = f"key from ${login.api_key_env}"
        else:
            where = "no key stored"
        return f"{login.provider} / {model} ({where})"

    def has_secret(self) -> bool:
        """Whether a key is stored, without validating the rest of the record."""
        try:
            login = self._parse()
        except ProviderLoginError:
            return False
        return login is not None and bool(login.api_key)
