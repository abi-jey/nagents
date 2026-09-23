"""Provider sign-in helpers shared by the terminal client and the TUI.

OpenRouter supports headless PKCE: the browser shows a single-use code, the
user pastes it, and ngn exchanges it for a user-controlled API key. The key is
returned only to the caller, which stores it in the protected credential store;
it never enters messages, diagnostics, or terminal output. ChatGPT/Codex keeps
its own device flow in ``auth.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import aiohttp

from nagents.provider import Provider
from nagents.provider.openai import USER_AGENT

from .config import PROVIDERS
from .credentials import ProviderLoginError

if TYPE_CHECKING:
    from collections.abc import Iterable

OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_DEFAULT_MODEL = "openrouter/auto"
_REQUEST_SECONDS = 30.0
_MAX_BYTES = 256 * 1024
# OpenRouter codes are short opaque strings; reject anything with controls or spaces.
_CODE_PATTERN = r"[\x21-\x7e]{8,512}"
_VERIFIER_PATTERN = r"[A-Za-z0-9\-._~]{43,128}"
_KEY_PATTERN = r"[\x21-\x7e]{8,8192}"
_EXCHANGE_FAILED = "OpenRouter sign-in could not be completed; run ngn login openrouter again."


@dataclass(frozen=True)
class LoginMethod:
    """One selectable sign-in path; never contains credentials."""

    token: str
    label: str
    provider: str
    env: str
    default_model: str
    summary: str
    needs_base_url: bool = False

    @property
    def device(self) -> bool:
        return self.token == "chatgpt"

    @property
    def pkce(self) -> bool:
        return self.token == "openrouter"


LOGIN_METHODS: tuple[LoginMethod, ...] = (
    LoginMethod(
        "chatgpt",
        "ChatGPT / Codex device login",
        "openai",
        "",
        "",
        "Use eligible ChatGPT subscription access for Codex; not general OpenAI API access.",
    ),
    LoginMethod(
        "openrouter",
        "OpenRouter browser sign-in",
        "openrouter",
        "OPENROUTER_API_KEY",
        OPENROUTER_DEFAULT_MODEL,
        "Authorize in a browser and store a user-controlled OpenRouter API key.",
    ),
    LoginMethod(
        "openai",
        "OpenAI Platform API key",
        "openai",
        "OPENAI_API_KEY",
        "gpt-4.1",
        "Usage-based OpenAI API billing, separate from a ChatGPT subscription.",
    ),
    LoginMethod(
        "anthropic",
        "Anthropic API key",
        "anthropic",
        "ANTHROPIC_API_KEY",
        "",
        "Usage-based Anthropic API billing; enter a model ID.",
    ),
    LoginMethod(
        "gemini",
        "Google Gemini API key",
        "gemini",
        "GEMINI_API_KEY",
        "",
        "Google AI Studio API key; enter a model ID.",
    ),
    LoginMethod(
        "custom",
        "Custom OpenAI-compatible endpoint",
        "openai_compatible",
        "OPENAI_API_KEY",
        "",
        "Your own gateway, Ollama, or another compatible service; requires a base URL and model.",
        needs_base_url=True,
    ),
)
_BY_TOKEN = {method.token: method for method in LOGIN_METHODS}
METHOD_TOKENS = ", ".join(entry.token for entry in LOGIN_METHODS)


def methods() -> tuple[LoginMethod, ...]:
    return LOGIN_METHODS


def method(token: str) -> LoginMethod:
    """Resolve a provider token from the login menu; never guesses."""
    chosen = _BY_TOKEN.get(token.strip().lower())
    if chosen is None:
        raise ValueError(f"Unknown sign-in method {token!r}; choose from {', '.join(_BY_TOKEN)}")
    return chosen


def method_for_provider(provider: str) -> LoginMethod:
    """Resolve a provider name, e.g. from --provider, to its sign-in method."""
    for candidate in LOGIN_METHODS:
        if candidate.provider == provider and not candidate.device:
            return candidate
    kind = PROVIDERS.get(provider)
    for candidate in LOGIN_METHODS:
        if not candidate.device and kind is not None and PROVIDERS[candidate.provider] is kind:
            return candidate
    raise ValueError(f"Cannot sign in to provider {provider!r}; choose from {METHOD_TOKENS}")


def generic(provider: str) -> LoginMethod:
    """Sign-in metadata for an advanced provider not on the standard menu."""
    kind = PROVIDERS.get(provider)
    if kind is None:
        raise ValueError(f"Unknown provider {provider!r}; choose from {', '.join(sorted(PROVIDERS))}")
    needs_base_url = provider in {"litellm", "azure_openai_compatible", "azure_openai_compatible_v1"}
    return LoginMethod(
        provider,
        f"{provider} API key",
        provider,
        "",
        "",
        "Advanced provider selection; enter the model and endpoint details ngn requires.",
        needs_base_url,
    )


def code_verifier() -> str:
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(verifier: str, *, label: str = "ngn") -> str:
    """Headless PKCE authorization URL; OpenRouter displays the code to paste."""
    query = urlencode(
        {
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
            "key_label": label,
        }
    )
    return f"{OPENROUTER_AUTH_URL}?{query}"


def valid_code(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(_CODE_PATTERN, value) is not None


async def exchange_code(code: str, verifier: str) -> str:
    """Exchange a pasted OpenRouter code for an API key; never logs either value."""
    if not valid_code(code) or re.fullmatch(_VERIFIER_PATTERN, verifier) is None:
        raise ProviderLoginError(_EXCHANGE_FAILED)
    payload = {"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_REQUEST_SECONDS),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            ) as client,
            client.post(
                OPENROUTER_KEYS_URL,
                json=payload,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                allow_redirects=False,
            ) as response,
        ):
            raw = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                raw.extend(chunk)
                if len(raw) > _MAX_BYTES:
                    raise ProviderLoginError(_EXCHANGE_FAILED)
            if response.status != 200:
                raise ProviderLoginError(_EXCHANGE_FAILED)
            data: object = json.loads(raw)
    except ProviderLoginError:
        raise
    except (TimeoutError, aiohttp.ClientError, ValueError, UnicodeError, RecursionError):
        raise ProviderLoginError(_EXCHANGE_FAILED) from None
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or re.fullmatch(_KEY_PATTERN, key) is None:
        raise ProviderLoginError(_EXCHANGE_FAILED)
    return key


async def verify_credentials(*, provider: str, model: str, base_url: str, api: str, api_key: str) -> str:
    """Best-effort catalog check; never fails a completed sign-in or echoes a key."""
    try:
        candidate = Provider(
            provider_type=PROVIDERS[provider],
            api_key=api_key,
            model=model,
            base_url=base_url or None,
            api=api,
        )
    except Exception:
        return "Credentials were saved, but the selection could not be verified; check it before the first request."
    try:
        models = await candidate.get_model_list()
    except NotImplementedError:
        return "This provider does not expose a model catalog; the first live request is authoritative."
    except Exception:
        return "Credentials were saved, but live verification failed; the first request will confirm access."
    finally:
        await candidate.close()
    if model in models or f"models/{model}" in models:
        return f"Verified: {len(models)} models are visible to this credential."
    return f"Credentials were saved, but model {model!r} was not found in the provider catalog ({len(models)} models)."


def format_methods(entries: Iterable[LoginMethod] | None = None) -> str:
    """Plain-text menu shared by the CLI; contains no secrets."""
    selected = list(entries or LOGIN_METHODS)
    return "\n".join(f"  {index}. {entry.label} - {entry.summary}" for index, entry in enumerate(selected, 1))
