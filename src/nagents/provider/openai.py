"""OpenAI API-key and ChatGPT authentication with local configuration discovery.

API-key configurations use the normal Provider HTTP contract. ChatGPT credentials
only go to fixed Codex endpoints, without base Provider logging or retries. The
OAuth transport releases tools only after a validated completed response;
GenerationConfig.reasoning.enabled requests a summary, while sampling settings
and token budgets are unsupported on that route.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from dataclasses import field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version
from pathlib import Path
from time import time
from typing import TYPE_CHECKING
from typing import cast

import aiohttp

from ..adapters._validation import list_data
from ..events import ErrorEvent
from ..events import FinishReason
from ..events import ReasoningChunkEvent
from ..events import TextChunkEvent
from ..events import TextDoneEvent
from ..events import ToolCallEvent
from ..events import Usage
from ..exceptions import ModelListError
from ..observation import response_chunk
from ..observation import trace_config
from ..types import COMPACTION_SUMMARY_PREFIX
from ..types import ImageContent
from ..types import TextContent
from .base import Provider
from .base import ProviderType
from .gateway import GatewayHTTPClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable

    from ..events import Event
    from ..live import LiveConfig
    from ..realtime import RealtimeConfig
    from ..types import GenerationConfig
    from ..types import Message
    from ..types import RetryConfig
    from ..types import ToolDefinition
    from .auth import BearerTokenProvider

DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
CODEX_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
CODEX_MODELS_ENDPOINT = "https://chatgpt.com/backend-api/codex/models"
# Catalog protocol compatibility, not ngn's version or client identity.
# Verified against openai/codex rust-v0.153.4 (3d2ee51ca2d5).
CODEX_MODELS_CLIENT_VERSION = "0.153.4"
try:
    USER_AGENT = f"ngn/{version('nagents')}"
except PackageNotFoundError:
    USER_AGENT = "ngn/unknown"

_MAX_EVENT_BYTES = 4 * 1024 * 1024
_MAX_STREAM_BYTES = 32 * 1024 * 1024
_MAX_ITEMS = 1024


@dataclass
class CodexCredentials:
    access_token: str = field(repr=False)
    account_id: str = field(default="", repr=False)
    residency: str = field(default="", repr=False)


class CodexConfigError(ValueError):
    """Local discovery failed; messages exclude credentials and file bodies."""


def _read_config_file(path: Path, *, toml: bool = False) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError
        value: object = tomllib.loads(raw.decode("utf-8")) if toml else json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return cast("dict[str, object]", value)
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError, OSError, RecursionError):
        raise CodexConfigError("Cannot read Codex configuration or credentials") from None


def _config_table(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CodexConfigError("Expected a table in Codex configuration")
    return cast("dict[str, object]", value)


def _config_string(value: object) -> str:
    if not isinstance(value, str):
        raise CodexConfigError("Expected a string in Codex configuration")
    return value


def _merge_config(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_config(_config_table(result[key]), _config_table(value))
        else:
            result[key] = value
    return result


def _token_claims(token: str) -> dict[str, object]:
    try:
        part = token.split(".")[1]
        return _config_table(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))))
    except (IndexError, ValueError, UnicodeError, RecursionError):
        return {}


def _local_credentials(home: Path) -> CodexCredentials:
    auth = _read_config_file(home / "auth.json")
    if auth.get("auth_mode") not in (None, "chatgpt"):
        raise CodexConfigError("Codex login method changed; recreate the provider")
    tokens = _config_table(auth.get("tokens", {}))
    access = _config_string(tokens.get("access_token", ""))
    if not access:
        raise CodexConfigError("No Codex ChatGPT access token; run codex login")
    claims = _token_claims(access)
    expires = claims.get("exp")
    if isinstance(expires, int | float) and expires <= time():
        raise CodexConfigError("Codex access token expired; refresh the login with Codex")
    identity = _token_claims(_config_string(tokens.get("id_token", "")))
    routing = _config_table(claims.get("https://api.openai.com/auth", {}))
    identity_routing = _config_table(identity.get("https://api.openai.com/auth", {}))
    account = (
        tokens.get("account_id") or routing.get("chatgpt_account_id") or identity_routing.get("chatgpt_account_id", "")
    )
    residency = routing.get("chatgpt_compute_residency", "")
    return CodexCredentials(
        access, _config_string(account), "" if residency == "no_constraint" else _config_string(residency)
    )


@dataclass
class _CodexConfig:
    model: str
    api: str
    home: Path
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    oauth: bool = False
    workspace: str = ""

    async def credentials(self) -> CodexCredentials:
        current = _local_credentials(self.home)
        if self.workspace and current.account_id != self.workspace:
            raise CodexConfigError("Codex login does not match the configured workspace")
        return current


def _load_config(home: Path | str = "", *, profile: str = "", model: str = "", for_live: bool = False) -> _CodexConfig:
    """Resolve explicit home > CODEX_HOME > ~/.codex; never load project config."""
    directory = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    config = _read_config_file(directory / "config.toml", toml=True)
    selected = profile or _config_string(config.get("profile", ""))
    if selected:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", selected):
            raise CodexConfigError("Invalid Codex profile name")
        profile_path = directory / f"{selected}.config.toml"
        if profile_path.exists():
            override = _read_config_file(profile_path, toml=True)
        else:
            profiles = _config_table(config.get("profiles", {}))
            if selected not in profiles:
                raise CodexConfigError("Selected Codex profile was not found")
            override = _config_table(profiles[selected])
        config = _merge_config(config, override)
    model = model or _config_string(config.get("model", DEFAULT_CODEX_MODEL))
    provider_id = _config_string(config.get("model_provider", "openai"))
    providers = _config_table(config.get("model_providers", {}))
    if provider_id != "openai" and provider_id not in providers:
        raise CodexConfigError("Selected Codex model provider is not configured")
    provider = _config_table(providers.get(provider_id, {}))
    wire_api = _config_string(provider.get("wire_api", "responses"))
    api = {"responses": "responses", "chat": "chat_completions"}.get(wire_api)
    if api is None:
        raise CodexConfigError("Unsupported Codex wire_api; expected responses or legacy chat")
    if any(provider.get(key) for key in ("auth", "http_headers", "env_http_headers", "query_params")):
        raise CodexConfigError(
            "Command auth and custom headers/query parameters require an explicitly configured provider"
        )
    requires_auth = provider.get("requires_openai_auth", provider_id == "openai")
    if not isinstance(requires_auth, bool):
        raise CodexConfigError("Codex requires_openai_auth must be boolean")
    base_url = _config_string(provider.get("base_url", config.get("openai_base_url", "")))
    if requires_auth:
        store = config.get("cli_auth_credentials_store", "file")
        if store not in ("file", "auto"):
            raise CodexConfigError("Codex discovery requires file credentials in CODEX_HOME/auth.json")
        auth = _read_config_file(directory / "auth.json")
        mode = auth.get("auth_mode")
        oauth = mode == "chatgpt" or (mode is None and bool(auth.get("tokens")))
        if oauth and for_live:
            # Matches Codex's voice auth selection: ChatGPT text login can use
            # an API-key fallback for voice, never its subscription access token.
            voice_key = _config_string(auth.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", ""))
            if not voice_key:
                raise CodexConfigError(
                    "Codex selected ChatGPT authentication, but GPT-Live voice requires an OpenAI API key. "
                    "No cached API key or OPENAI_API_KEY fallback was found. The saved ChatGPT login can "
                    "still run the delegated backend."
                )
            return _CodexConfig(model, api, directory, base_url=base_url, api_key=voice_key)
        if oauth:
            if base_url or config.get("chatgpt_base_url"):
                raise CodexConfigError("Codex ChatGPT login requires the standard Codex endpoint")
            if config.get("forced_login_method") == "api":
                raise CodexConfigError("Codex configuration requires API-key login")
            credentials = _local_credentials(directory)
            workspace = config.get("forced_chatgpt_workspace_id", "")
            if workspace and credentials.account_id != workspace:
                raise CodexConfigError("Codex login does not match the configured workspace")
            return _CodexConfig(model, "responses", directory, oauth=True, workspace=_config_string(workspace))
        if config.get("forced_login_method") == "chatgpt":
            raise CodexConfigError("Codex configuration requires ChatGPT login")
        if mode not in (None, "apikey"):
            raise CodexConfigError("Unsupported Codex login method")
        key = _config_string(auth.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", ""))
    else:
        env_key = _config_string(provider.get("env_key", ""))
        key = os.environ.get(env_key, "") if env_key else _config_string(provider.get("experimental_bearer_token", ""))
        if not env_key and not key:
            raise CodexConfigError(
                "This Codex provider has no key; configure an explicit Nagents provider for local inference"
            )
    if not key:
        raise CodexConfigError("Codex API credential is missing; check auth.json or the provider's env_key")
    return _CodexConfig(model, api, directory, base_url=base_url, api_key=key)


class _ProtocolError(ValueError):
    """Only constant, safe messages belong in this exception."""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _ProtocolError("Codex returned malformed response data; retry the request.")
    return cast("dict[str, object]", value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise _ProtocolError("Codex returned malformed response text; retry the request.")
    return value


def _text(message: Message) -> str:
    if message.content is None:
        return ""
    if isinstance(message.content, str):
        return message.content
    if not all(isinstance(part, TextContent) for part in message.content):
        raise _ProtocolError("Codex supports images only in user messages; audio and documents are unsupported.")
    return "\n".join(part.text for part in message.content if isinstance(part, TextContent))


def _request_body(
    model: str, messages: list[Message], tools: list[ToolDefinition] | None, config: GenerationConfig | None
) -> dict[str, object]:
    instructions: list[str] = []
    inputs: list[dict[str, object]] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            instructions.append(_text(message))
        elif message.role == "tool":
            if not message.tool_call_id:
                raise _ProtocolError("A tool result is missing its call ID; repair the session history.")
            inputs.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": _text(message)})
        else:
            role = "user" if message.role == "compaction_summary" else message.role
            if role not in {"user", "assistant"}:
                raise _ProtocolError("Codex received an unsupported message role.")
            content: list[dict[str, object]] = []
            if message.role == "compaction_summary":
                content.append({"type": "input_text", "text": COMPACTION_SUMMARY_PREFIX + _text(message)})
            elif isinstance(message.content, list):
                for part in message.content:
                    if isinstance(part, TextContent):
                        content.append(
                            {"type": "output_text" if role == "assistant" else "input_text", "text": part.text}
                        )
                    elif isinstance(part, ImageContent) and role == "user":
                        if part.media_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                            raise _ProtocolError("Codex supports PNG, JPEG, WebP, and GIF images only.")
                        content.append(
                            {
                                "type": "input_image",
                                "image_url": f"data:{part.media_type};base64,{part.base64_data}",
                                "detail": part.detail or "auto",
                            }
                        )
                    else:
                        raise _ProtocolError("Codex supports user images and text, not audio or document attachments.")
            elif message.content is not None:
                content.append(
                    {"type": "output_text" if role == "assistant" else "input_text", "text": message.content}
                )
            if content:
                inputs.append({"role": role, "content": content})
            for call in message.tool_calls:
                if role != "assistant" or not call.id or not call.name:
                    raise _ProtocolError("Invalid assistant tool call in session history.")
                inputs.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, allow_nan=False),
                    }
                )
    body: dict[str, object] = {
        "model": model,
        "instructions": "\n\n".join(instructions),
        "input": inputs,
        "store": False,
        "stream": True,
        "tools": [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": False,
            }
            for tool in tools or []
        ],
    }
    if config and config.reasoning and config.reasoning.get("enabled"):
        body["reasoning"] = {"summary": "auto"}
    return body


async def _sse(response: aiohttp.ClientResponse) -> AsyncIterator[dict[str, object]]:
    buffer = b""
    data: list[bytes] = []
    frame_size = 0
    total = 0
    async for chunk in response.content.iter_chunked(8192):
        total += len(chunk)
        if total > _MAX_STREAM_BYTES:
            raise _ProtocolError("Codex response exceeded the safe stream size limit.")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            frame_size += len(line)
            if frame_size > _MAX_EVENT_BYTES:
                raise _ProtocolError("Codex response exceeded the safe event size limit.")
            if not line:
                if data:
                    payload = b"\n".join(data)
                    response_chunk(payload.decode("utf-8", errors="replace"))
                    if payload == b"[DONE]":
                        return
                    try:
                        parsed: object = json.loads(payload)
                    except (ValueError, UnicodeError, RecursionError):
                        raise _ProtocolError("Codex returned malformed stream JSON; retry the request.") from None
                    yield _object(parsed)
                data = []
                frame_size = 0
            elif line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
        if len(buffer) + frame_size > _MAX_EVENT_BYTES:
            raise _ProtocolError("Codex response exceeded the safe event size limit.")
    if data or buffer.strip():
        raise _ProtocolError("Codex stream ended inside an event; retry the request.")


def _index(value: object) -> int:
    if type(value) is not int or not 0 <= value < _MAX_ITEMS:
        raise _ProtocolError("Codex returned an invalid output index.")
    return value


def _usage(value: object) -> Usage:
    usage = _object(value) if value is not None else {}

    def count(data: dict[str, object], name: str) -> int:
        result = data.get(name, 0)
        if type(result) is not int or result < 0:
            raise _ProtocolError("Codex returned invalid token usage.")
        return result

    return Usage(
        prompt_tokens=count(usage, "input_tokens"),
        completion_tokens=count(usage, "output_tokens"),
        total_tokens=count(usage, "total_tokens")
        if "total_tokens" in usage
        else count(usage, "input_tokens") + count(usage, "output_tokens"),
        cached_tokens=count(_object(usage.get("input_tokens_details") or {}), "cached_tokens"),
        reasoning_tokens=count(_object(usage.get("output_tokens_details") or {}), "reasoning_tokens"),
    )


class OpenAIProvider(Provider):
    """OpenAI inference with explicit credentials or automatic local discovery.

    An explicit credential callback selects the existing OAuth transport. On that
    route, verify_model is local and no automatic retry/replay follows failures.
    API-key configurations use the selected standard Provider API contract.
    """

    def __init__(
        self,
        credentials: Callable[[], Awaitable[CodexCredentials]] | None = None,
        model: str = "",
        timeout: float = 120.0,
        *,
        home: str | Path = "",
        profile: str = "",
        live_config: LiveConfig | None = None,
        api_key: str = "",
        base_url: str = "",
        api: str = "auto",
        realtime_config: RealtimeConfig | None = None,
        retry_config: RetryConfig | None = None,
        bearer_token_provider: BearerTokenProvider | None = None,
    ) -> None:
        """Explicit API keys/token providers bypass discovery; otherwise use CODEX_HOME/~/.codex.

        Local model/profile/wire_api and auth.json select API-key or ChatGPT
        authentication. Explicit credential callbacks select ChatGPT. A custom
        base_url requires an explicit key or bearer_token_provider; saved OAuth never goes to that URL.
        API-key requests default to Responses unless another API is selected.
        """
        self._local_api = False
        self._credentials: Callable[[], Awaitable[CodexCredentials]]
        if sum((credentials is not None, bool(api_key), bearer_token_provider is not None)) > 1:
            raise ValueError("Choose api_key, bearer_token_provider, or ChatGPT credentials, not multiple auth sources")
        if base_url and not api_key and bearer_token_provider is None:
            raise ValueError("An explicit base_url requires an explicit api_key or bearer_token_provider")
        if not api_key and credentials is None and bearer_token_provider is None:
            local = _load_config(
                home, profile=profile, model=model, for_live=live_config is not None or realtime_config is not None
            )
            model = local.model
            credentials = local.credentials
            if not local.oauth:
                api_key = local.api_key
                base_url = local.base_url
                api = local.api if api == "auto" else api
        if api_key or bearer_token_provider is not None:
            super().__init__(
                ProviderType.OPENAI_COMPATIBLE,
                api_key,
                model or DEFAULT_CODEX_MODEL,
                base_url=base_url or None,
                api="responses" if api == "auto" else api,
                timeout=timeout,
                live_config=live_config,
                realtime_config=realtime_config,
                retry_config=retry_config,
                bearer_token_provider=bearer_token_provider,
            )
            self._local_api = True
            return
        assert credentials is not None
        if api not in {"auto", "responses"}:
            raise ValueError("ChatGPT authentication uses the Responses protocol")
        if live_config is not None or realtime_config is not None:
            raise ValueError(
                "Selected ChatGPT subscription authentication. GPT-Live and Realtime require "
                "OpenAI API-key authentication; this saved login can still run the delegated backend."
            )
        super().__init__(
            ProviderType.OPENAI_COMPATIBLE,
            "oauth-not-an-api-key",
            model or DEFAULT_CODEX_MODEL,
            timeout=timeout,
            api="responses",
        )
        self.base_url = CODEX_ENDPOINT
        self._credentials = credentials
        self._timeout = timeout

    @property
    def uses_chatgpt_auth(self) -> bool:
        """Whether this instance uses the subscription transport, not an API key."""
        return not self._local_api

    async def verify_model(self, force: bool = False) -> bool:
        if self._local_api:
            return await super().verify_model(force)
        self._model_verified = True
        return True

    async def get_model_list(self) -> list[str]:
        """Fetch model IDs from the API-key catalog or the subscription catalog.

        With ChatGPT authentication this follows the Codex client's catalog contract, not the public
        OpenAI API-key catalog. It is fresh, read-only, and independent of model
        verification. Failures raise ModelListError without upstream data; there
        is no API-key fallback or entitlement guarantee.
        """
        if self._local_api:
            return await super().get_model_list()
        if self.base_url != CODEX_ENDPOINT:
            raise ModelListError("Codex model discovery requires its fixed endpoint; custom URLs are unsupported.")
        try:
            credentials = await self._credentials()
            # Capture all routing fields before any further await, even when the
            # callback returns a mutable credentials object shared with its caller.
            access_token, account_id, residency = (
                credentials.access_token,
                credentials.account_id,
                credentials.residency,
            )
            if not access_token or not re.fullmatch(r"[\x21-\x7e]{1,65536}", access_token):
                raise ValueError
            if account_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", account_id):
                raise ValueError
            if residency and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", residency):
                raise ValueError
        except CodexConfigError as error:
            raise ModelListError(str(error)) from None
        except Exception:
            raise ModelListError("ChatGPT credentials are unavailable; sign in again with /login.") from None
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "version": CODEX_MODELS_CLIENT_VERSION,
            "originator": "ngn",
            "User-Agent": USER_AGENT,
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        if residency and residency != "no_constraint":
            headers["x-openai-internal-codex-residency"] = residency
        try:
            client = GatewayHTTPClient(timeout=self._model_list_timeout)
            async with client:
                response = await client.get_json(
                    f"{CODEX_MODELS_ENDPOINT}?client_version={CODEX_MODELS_CLIENT_VERSION}", headers
                )
        except Exception:
            raise ModelListError(
                "Codex model discovery failed; check service availability and your ChatGPT login."
            ) from None
        if "error" in response or not isinstance(response.get("models"), list):
            raise ModelListError("Codex returned an invalid model catalog.")
        models: list[str] = []
        seen: set[str] = set()
        for item in list_data(response["models"]):
            if not isinstance(item, dict):
                raise ModelListError("Codex returned an invalid model entry.")
            model_id, visibility = item.get("slug"), item.get("visibility")
            if (
                not isinstance(model_id, str)
                or not model_id.strip()
                or any(unicodedata.category(char).startswith("C") for char in model_id)
            ):
                raise ModelListError("Codex returned an invalid model ID.")
            # Visibility is required by the pinned upstream ModelInfo struct.
            if not isinstance(visibility, str) or visibility not in {"list", "hide", "none"}:
                raise ModelListError("Codex returned invalid model visibility.")
            # supported_in_api is not an OAuth availability filter.
            if visibility == "list" and model_id not in seen:
                models.append(model_id)
                seen.add(model_id)
        return models

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncGenerator[Event, None]:
        if self._local_api:
            async for generated in super().generate(messages, tools, config, stream, verify_model):
                yield generated
            return
        try:
            body = _request_body(self.model, messages, tools, config)
        except (TypeError, ValueError) as error:
            message = str(error) if isinstance(error, _ProtocolError) else "Codex request data is invalid."
            yield ErrorEvent(message=message, code="CODEX_REQUEST_INVALID")
            return
        try:
            credentials = await self._credentials()
            if not credentials.access_token or not re.fullmatch(r"[\x21-\x7e]{1,65536}", credentials.access_token):
                raise ValueError
            if credentials.account_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", credentials.account_id):
                raise ValueError
            if credentials.residency and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", credentials.residency):
                raise ValueError
        except CodexConfigError as error:
            yield ErrorEvent(message=str(error), code="CODEX_AUTH")
            return
        except Exception:
            yield ErrorEvent(
                message="ChatGPT credentials are unavailable; sign in again with /login.", code="CODEX_AUTH"
            )
            return
        headers = {
            "Authorization": f"Bearer {credentials.access_token}",
            "Accept": "text/event-stream",
            "originator": "ngn",
            "User-Agent": USER_AGENT,
        }
        if credentials.account_id:
            headers["ChatGPT-Account-Id"] = credentials.account_id
        if credentials.residency and credentials.residency != "no_constraint":
            headers["x-openai-internal-codex-residency"] = credentials.residency
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                    trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    trace_configs=[trace_config()],
                ) as client,
                client.post(CODEX_ENDPOINT, json=body, headers=headers, allow_redirects=False) as response,
            ):
                if response.status != 200:
                    messages_by_status = {
                        401: "ChatGPT login expired or was rejected; sign in again with /login.",
                        403: "Codex access denied; check your ChatGPT plan, workspace permissions, and model access.",
                        429: "ChatGPT Codex usage limit reached; wait before retrying or check your plan limits.",
                    }
                    yield ErrorEvent(
                        message=messages_by_status.get(
                            response.status,
                            f"Codex request failed (HTTP {response.status}); check service availability and model access.",
                        ),
                        code=f"CODEX_HTTP_{response.status}",
                    )
                    return
                # Some Codex responses omit Content-Type; _sse still validates framing
                # and no calls are released without a completed, validated response.
                if response.headers.get("Content-Type") and response.content_type != "text/event-stream":
                    raise _ProtocolError("Codex did not return an event stream; retry the request.")
                items: dict[int, dict[str, object]] = {}
                finished: set[int] = set()
                arguments: dict[int, str] = {}
                texts: dict[tuple[int, int], str] = {}
                completed = False
                usage = Usage()
                async for event in _sse(response):
                    kind = _string(event.get("type"))
                    if kind in {"error", "response.failed", "response.incomplete"}:
                        raise _ProtocolError(
                            "Codex response failed or was incomplete; no tool calls were released. Retry the request."
                        )
                    if kind in {"response.output_item.added", "response.output_item.done"}:
                        index = _index(event.get("output_index"))
                        item = _object(event.get("item"))
                        previous = items.get(index, {})
                        for key in ("id", "call_id", "name", "type"):
                            if key in previous and key in item and previous[key] != item[key]:
                                raise _ProtocolError("Codex returned inconsistent output item identities.")
                        items[index] = {**previous, **item}
                        if kind == "response.output_item.done":
                            finished.add(index)
                    elif kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
                        index = _index(event.get("output_index"))
                        item = items.setdefault(index, {})
                        if "item_id" in event and "id" in item and event["item_id"] != item["id"]:
                            raise _ProtocolError("Codex returned inconsistent argument item identities.")
                        if kind.endswith(".delta"):
                            arguments[index] = arguments.get(index, "") + _string(event.get("delta"))
                        else:
                            item["arguments"] = _string(event.get("arguments"))
                    elif kind in {"response.output_text.delta", "response.output_text.done"}:
                        text_key = (_index(event.get("output_index", 0)), _index(event.get("content_index", 0)))
                        if kind.endswith(".delta"):
                            delta = _string(event.get("delta"))
                            texts[text_key] = texts.get(text_key, "") + delta
                            if stream:
                                yield TextChunkEvent(chunk=delta)
                        else:
                            texts[text_key] = _string(event.get("text"))
                    elif kind == "response.reasoning_summary_text.delta":
                        delta = _string(event.get("delta"))
                        if stream:
                            yield ReasoningChunkEvent(chunk=delta)
                    elif kind == "response.completed":
                        result = _object(event.get("response"))
                        if result.get("status", "completed") != "completed" or result.get("error"):
                            raise _ProtocolError("Codex response was not completed; no tool calls were released.")
                        output = result.get("output")
                        if not isinstance(output, list) or len(output) > _MAX_ITEMS:
                            raise _ProtocolError("Codex completed response has malformed output.")
                        observed = set(items) | set(arguments) | {index for index, _ in texts}
                        # Codex may send output only in output_item.done, leaving the
                        # terminal response's output array empty to avoid duplication.
                        if not output and observed - finished:
                            raise _ProtocolError(
                                "Codex completed before streamed output finished; no tool calls were released."
                            )
                        if output and observed - set(range(len(output))):
                            raise _ProtocolError(
                                "Codex completed response omitted streamed output; no tool calls were released."
                            )
                        for index, raw in enumerate(output):
                            item = _object(raw)
                            previous = items.get(index, {})
                            for identity in ("id", "call_id", "name", "type"):
                                if identity in previous and identity in item and previous[identity] != item[identity]:
                                    raise _ProtocolError("Codex returned inconsistent completed output identities.")
                            items[index] = {**previous, **item}
                            finished.add(index)
                        usage = _usage(result.get("usage"))
                        completed = True
                        break
                if not completed:
                    raise _ProtocolError("Codex stream ended before response.completed; no tool calls were released.")
                calls: list[ToolCallEvent] = []
                call_ids: set[str] = set()
                for index, item in sorted(items.items()):
                    if item.get("type") == "function_call":
                        call_id, name = _string(item.get("call_id")), _string(item.get("name"))
                        if (
                            not call_id
                            or not name
                            or call_id in call_ids
                            or index not in finished
                            or item.get("status") not in (None, "completed")
                        ):
                            raise _ProtocolError("Codex returned an invalid or unfinished tool call.")
                        raw_arguments = _string(item.get("arguments", arguments.get(index, "")))
                        try:
                            parsed_args = _object(json.loads(raw_arguments))
                            # JSON serialization rejects non-finite values accepted by Python's decoder.
                            json.dumps(parsed_args, allow_nan=False)
                            if arguments.get(index) and _object(json.loads(arguments[index])) != parsed_args:
                                raise _ProtocolError("Codex returned inconsistent tool arguments.")
                        except (ValueError, UnicodeError, RecursionError):
                            raise _ProtocolError(
                                "Codex returned malformed tool arguments; no tool calls were released."
                            ) from None
                        call_ids.add(call_id)
                        calls.append(ToolCallEvent(id=call_id, name=name, arguments=parsed_args, usage=usage))
                    elif index in arguments:
                        raise _ProtocolError("Codex returned tool arguments without a complete tool call.")
                    elif item.get("type") == "message":
                        content = item.get("content")
                        if not isinstance(content, list):
                            raise _ProtocolError("Codex returned malformed message content.")
                        for part_index, raw_part in enumerate(content):
                            part = _object(raw_part)
                            if part.get("type") == "output_text":
                                texts[(index, part_index)] = _string(part.get("text"))
                            elif part.get("type") == "refusal":
                                texts[(index, part_index)] = _string(part.get("refusal"))
            # Finish network cleanup before releasing executable calls.
            yield TextDoneEvent(
                text="".join(text for _, text in sorted(texts.items())),
                usage=usage,
                finish_reason=FinishReason.TOOL_CALLS if calls else FinishReason.STOP,
            )
            for call in calls:
                yield call
        except _ProtocolError as error:
            yield ErrorEvent(message=str(error), code="CODEX_STREAM_INVALID")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            yield ErrorEvent(
                message="Codex connection failed or timed out; retry the request.", code="CODEX_CONNECTION"
            )
