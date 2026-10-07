"""Named ngn provider connections shared by terminal and web clients.

Only routing and environment-variable *names* are persisted. OAuth caches and
Codex's own configuration are discovered at runtime and are not copied here.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Protocol
from typing import cast
from urllib.parse import urlsplit

from nagents.provider import ProviderType
from nagents.provider.auth import validate_prefix
from nagents.provider.openai import CODEX_ENDPOINT

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Mapping
    from typing import BinaryIO

NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
REVISION = re.compile(r"[0-9a-f]{64}\Z")
MAX_BYTES = 64 * 1024
PROVIDERS = {provider.value: provider for provider in ProviderType}
PROVIDERS.update(
    openai=ProviderType.OPENAI_COMPATIBLE,
    gemini=ProviderType.GEMINI_NATIVE,
    google=ProviderType.GEMINI_NATIVE,
    azure=ProviderType.AZURE_OPENAI_COMPATIBLE,
    foundry=ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
)
API_NAMES: tuple[str, ...] = ("auto", "chat_completions", "responses", "messages", "completions")


class _WindowsLock(Protocol):
    LK_LOCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int) -> None: ...


@contextmanager
def _exclusive_lock(stream: BinaryIO) -> Iterator[None]:
    """Lock the same sentinel byte on Windows, or the whole file on POSIX."""
    if os.name == "nt":
        windows = cast("_WindowsLock", importlib.import_module("msvcrt"))

        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        windows.locking(stream.fileno(), windows.LK_LOCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            windows.locking(stream.fileno(), windows.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@dataclass(frozen=True)
class ProviderKind:
    label: str
    auth: tuple[str, ...]
    apis: tuple[str, ...]
    env: str
    endpoint_required: bool = False
    version_required: bool = False
    live: bool = False


KINDS: dict[str, ProviderKind] = {
    "openai": ProviderKind(
        "OpenAI",
        ("auto", "api-key", "chatgpt", "codex"),
        ("auto", "responses", "chat_completions"),
        "OPENAI_API_KEY",
        live=True,
    ),
    "openai_compatible": ProviderKind(
        "OpenAI-compatible",
        ("api-key",),
        ("auto", "responses", "chat_completions", "messages"),
        "OPENAI_API_KEY",
        endpoint_required=True,
        live=True,
    ),
    "foundry": ProviderKind(
        "Azure AI Foundry",
        ("entra", "api-key"),
        ("responses", "chat_completions"),
        "FOUNDRY_API_KEY",
        endpoint_required=True,
        live=True,
    ),
    "azure_openai_compatible_v1": ProviderKind(
        "Azure OpenAI v1",
        ("entra", "api-key"),
        ("auto", "responses", "chat_completions"),
        "AZURE_OPENAI_API_KEY",
        endpoint_required=True,
        live=True,
    ),
    "azure_openai_compatible": ProviderKind(
        "Azure OpenAI (versioned)",
        ("api-key",),
        ("auto", "chat_completions"),
        "AZURE_OPENAI_API_KEY",
        endpoint_required=True,
        version_required=True,
    ),
    "openrouter": ProviderKind(
        "OpenRouter", ("api-key",), ("auto", "chat_completions", "responses"), "OPENROUTER_API_KEY"
    ),
    "anthropic": ProviderKind("Anthropic", ("api-key",), ("auto", "messages"), "ANTHROPIC_API_KEY"),
    "gemini": ProviderKind("Gemini", ("api-key",), ("auto",), "GEMINI_API_KEY"),
    "litellm": ProviderKind(
        "LiteLLM",
        ("api-key",),
        ("auto", "chat_completions", "responses", "messages"),
        "LITELLM_API_KEY",
        endpoint_required=True,
    ),
}

DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}


def env_name(value: str) -> str:
    """Accept NAME or ${NAME}; reject literal keys and other interpolation."""
    if value.startswith("${") and value.endswith("}"):
        value = value[2:-1]
    if not ENV.fullmatch(value):
        raise ValueError("Use an environment variable name or ${NAME}, never a literal API key")
    return value


def validate_request_timeout(value: object) -> float:
    """A model HTTP deadline, independent of shell or whole-run budgets."""
    message = "request_timeout must be a positive finite number of seconds"
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(message)
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        raise ValueError(message) from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(message)
    return seconds


@dataclass(frozen=True)
class ProviderProfile:
    kind: str
    auth: str = "api-key"
    base_url: str = ""
    api: str = "auto"
    api_key_env: str = ""
    api_version: str = ""
    scope: str = "https://ai.azure.com/.default"
    request_timeout: float = 120.0

    def validate(self) -> None:
        if any(
            not isinstance(getattr(self, name), str)
            for name in ("kind", "auth", "base_url", "api", "api_key_env", "api_version", "scope")
        ):
            raise ValueError("Provider fields must be strings")
        validate_request_timeout(self.request_timeout)
        spec = KINDS.get(self.kind)
        if spec is None or self.auth not in spec.auth or self.api not in spec.apis:
            raise ValueError("Unsupported provider, authentication mode, or API")
        if spec.endpoint_required and not self.base_url:
            raise ValueError("This provider requires an API prefix URL")
        if self.kind == "openai" and self.base_url:
            raise ValueError("OpenAI uses its fixed API host; choose OpenAI-compatible for a custom endpoint")
        if self.base_url:
            validate_prefix(self.base_url)
            if len(self.base_url) > 2048:
                raise ValueError("API prefix is too long")
        if spec.version_required and not self.api_version:
            raise ValueError("This Azure route requires an API version")
        if self.api_version and not spec.version_required:
            raise ValueError("This provider does not use an API version")
        if self.api_version and (len(self.api_version) > 100 or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.api_version)):
            raise ValueError("Invalid API version")
        if self.kind not in {"foundry", "azure_openai_compatible_v1"} and self.scope != "https://ai.azure.com/.default":
            raise ValueError("Only Foundry accepts a token scope")
        if not self.scope or len(self.scope) > 256 or any(char.isspace() for char in self.scope):
            raise ValueError("Invalid token scope")
        if self.auth in {"api-key", "auto"} or (self.kind == "openai" and self.auth in {"chatgpt", "codex"}):
            env_name(self.api_key_env or spec.env)
        elif self.api_key_env:
            raise ValueError("This authentication mode does not use an API key")
        if self.auth in {"chatgpt", "codex"} and (self.base_url or self.api != "auto"):
            raise ValueError("ChatGPT/Codex requires the default endpoint and API")

    @property
    def key_env(self) -> str:
        return env_name(self.api_key_env or KINDS[self.kind].env)

    @property
    def effective_endpoint(self) -> str:
        if self.kind == "openai":
            if self.auth == "chatgpt":
                return CODEX_ENDPOINT
            if self.auth == "codex":
                return "Selected by local Codex configuration (ChatGPT Codex or configured API endpoint)"
            if self.auth == "auto":
                return f"Auth-dependent: {CODEX_ENDPOINT}, local Codex configuration, or {DEFAULT_ENDPOINTS['openai']}"
        return self.base_url or DEFAULT_ENDPOINTS.get(self.kind, "")

    @property
    def credential_source(self) -> str:
        if self.auth == "entra":
            return "Microsoft Entra ID (DefaultAzureCredential)"
        if self.auth == "codex":
            return "Local Codex configuration and credentials"
        if self.auth == "chatgpt":
            return "ChatGPT device login"
        if self.auth == "auto":
            return f"ChatGPT / Codex / API key ${self.key_env}"
        return f"API key ${self.key_env}"


def validate_provider(profile: ProviderProfile) -> None:
    """Validate a runtime provider profile, including programmatic harness routes."""
    if profile.kind not in PROVIDERS:
        raise ValueError(f"Unknown provider {profile.kind!r}; choose from {', '.join(sorted(PROVIDERS))}")
    if profile.api not in API_NAMES:
        raise ValueError(f"api must be one of: {', '.join(API_NAMES)}")
    if profile.kind == "litellm" and not profile.base_url:
        raise ValueError("LiteLLM requires an explicit base_url pointing to your gateway")
    if profile.auth not in {"auto", "api-key", "chatgpt", "codex", "entra"}:
        raise ValueError("auth must be auto, api-key, chatgpt, codex, or entra")
    if profile.auth == "entra" and profile.kind not in {"foundry", "azure_openai_compatible_v1"}:
        raise ValueError("Entra authentication requires Foundry or Azure v1")
    if profile.auth == "chatgpt" and (
        profile.kind not in {"openai", "openai_compatible"} or profile.base_url or profile.api != "auto"
    ):
        raise ValueError(
            "ChatGPT login requires the default OpenAI provider endpoint, without base_url or api overrides"
        )
    if profile.auth == "codex" and (profile.kind != "openai" or profile.base_url or profile.api != "auto"):
        raise ValueError("Local Codex discovery requires the default OpenAI endpoint and API")
    if not ENV.fullmatch(profile.key_env):
        raise ValueError("api_key_env must be an environment variable name, not a literal secret")
    if profile.base_url:
        url = urlsplit(profile.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or any(char.isspace() or ord(char) < 32 for char in profile.base_url)
        ):
            raise ValueError("base_url must be an HTTP(S) URL")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("base_url must not contain credentials, query parameters, or fragments")


@dataclass(frozen=True)
class ProviderRegistry:
    revision: str = "0" * 64
    active: str = ""
    providers: dict[str, ProviderProfile] = field(default_factory=dict)

    def validate(self, *, allow_external_active: bool = False) -> None:
        if (
            not isinstance(self.revision, str)
            or not isinstance(self.active, str)
            or not isinstance(self.providers, dict)
        ):
            raise ValueError("Invalid provider registry fields")
        if not REVISION.fullmatch(self.revision) or (
            self.active and self.active not in self.providers and not allow_external_active
        ):
            raise ValueError("Invalid provider registry selection")
        if len(self.providers) > 64:
            raise ValueError("Too many provider connections")
        for name, profile in self.providers.items():
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise ValueError("Provider connection names must be lowercase letters, digits, '-' or '_'")
            if not isinstance(profile, ProviderProfile):
                raise ValueError("Invalid provider connection")
            profile.validate()

    def snapshot(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "active": self.active,
            "providers": {
                name: {
                    **asdict(profile),
                    "key_configured": bool(os.environ.get(profile.key_env)),
                    "credential_source": profile.credential_source,
                    "effective_endpoint": profile.effective_endpoint,
                }
                for name, profile in self.providers.items()
            },
            "kinds": {name: asdict(spec) for name, spec in KINDS.items()},
        }


class ModelPreferenceStore:
    """Scoped chat-model choice inside config.json, separate from connections."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _document(self) -> dict[str, object]:
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("Configuration must be a regular, bounded file")
            raw = self.path.read_bytes()
            if len(raw) > MAX_BYTES:
                raise ValueError("Configuration is too large")
            data: object = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Invalid configuration format")
            return data
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, UnicodeError, OSError):
            raise ValueError("Configuration is unreadable or invalid") from None

    def load(self) -> str:
        return self._model(self._document())

    @staticmethod
    def _model(document: dict[str, object]) -> str:
        model = document.get("model", "")
        if not isinstance(model, str) or (model and (not model.strip() or len(model) > 200 or not model.isprintable())):
            raise ValueError("Invalid model preference")
        return model

    def save(self, model: str, *, only_if_missing: bool = False) -> str:
        if not isinstance(model, str) or (model and (not model.strip() or len(model) > 200 or not model.isprintable())):
            raise ValueError("Invalid model preference")
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = directory / ".models.lock"
        if lock.is_symlink():
            raise ValueError("Model preference lock must not be a symlink")
        with lock.open("a+b") as stream, _exclusive_lock(stream):
            document = self._document()
            current = self._model(document)
            if (only_if_missing and "model" in document) or ("model" in document and current == model):
                return current
            if model:
                document["model"] = model
            else:
                document.pop("model", None)
            payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if len(payload) > MAX_BYTES:
                raise ValueError("Configuration is too large")
            descriptor, temporary = tempfile.mkstemp(prefix=".models-", dir=directory)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    if os.name != "nt":
                        os.fchmod(output.fileno(), 0o600)
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
                if os.name != "nt":
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return model


class ProviderRegistryStore:
    """Revisioned, atomic JSON registry shared by ngn serve, ngn, and ngn run."""

    def __init__(self, path: Path | None = None, *, allow_external_active: bool = False) -> None:
        home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if not home.is_absolute():
            raise ValueError("XDG_CONFIG_HOME must be an absolute path")
        self.path = path or home / "ngn" / "providers.json"
        if self.path.suffix != ".json":
            raise ValueError("Provider registry path must end in .json")
        self.allow_external_active = allow_external_active

    @property
    def model_store(self) -> ModelPreferenceStore:
        return ModelPreferenceStore(self.path.with_name("config.json"))

    def load(self) -> ProviderRegistry:
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("Provider configuration must be a regular, bounded file")
            raw = self.path.read_bytes()
            if len(raw) > MAX_BYTES:
                raise ValueError("Provider configuration is too large")
            document: object = json.loads(raw)
            if (
                not isinstance(document, dict)
                or set(document) != {"version", "revision", "active", "providers"}
                or type(document["version"]) is not int
                or document["version"] != 2
            ):
                raise ValueError("Invalid provider configuration format")
            entries = document["providers"]
            if not isinstance(entries, dict) or len(entries) > 64:
                raise ValueError("Invalid provider entries")
            profiles: dict[str, ProviderProfile] = {}
            for name, value in entries.items():
                if (
                    not isinstance(name, str)
                    or not isinstance(value, dict)
                    or set(value) - set(ProviderProfile.__dataclass_fields__)
                ):
                    raise ValueError("Invalid provider entry")
                profiles[name] = ProviderProfile(**value)
            registry = ProviderRegistry(document["revision"], document["active"], profiles)
            registry.validate(allow_external_active=self.allow_external_active)
            return registry
        except FileNotFoundError:
            return ProviderRegistry()
        except (json.JSONDecodeError, TypeError, UnicodeError, OSError):
            raise ValueError("Provider configuration is unreadable or invalid") from None

    def save(self, registry: ProviderRegistry, *, expected: str) -> ProviderRegistry:
        """Reject stale revisions before atomically replacing the file."""
        registry.validate(allow_external_active=self.allow_external_active)
        if not REVISION.fullmatch(expected):
            raise ValueError("Invalid provider registry revision")
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = directory / ".providers.lock"
        if lock.is_symlink():
            raise ValueError("Provider configuration lock must not be a symlink")
        with lock.open("a+b") as stream, _exclusive_lock(stream):
            if self.load().revision != expected:
                raise ValueError("Provider configuration changed; reload before saving")
            next_registry = ProviderRegistry(secrets.token_hex(32), registry.active, registry.providers)
            document = {
                "version": 2,
                "revision": next_registry.revision,
                "active": next_registry.active,
                "providers": {name: asdict(profile) for name, profile in next_registry.providers.items()},
            }
            payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if len(payload) > MAX_BYTES:
                raise ValueError("Provider configuration is too large")
            descriptor, temporary = tempfile.mkstemp(prefix=".providers-", dir=directory)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    if os.name != "nt":
                        os.fchmod(output.fileno(), 0o600)
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                # load() rejects symlinks and oversized old destinations.
                self.load()
                os.replace(temporary, self.path)
                if os.name != "nt":
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return next_registry


class ScopedProviderRegistryStore:
    """Global connections plus a workspace-local JSON overlay and selection.

    Workspace files live under the user's configuration directory rather than
    inside an untrusted project. Both UIs and the terminal resolve the same two
    files for a workspace. Local names cannot shadow global names through the UI.
    """

    def __init__(self, workspace: Path, *, paths: Mapping[str, Path] | None = None) -> None:
        paths = paths or {}
        default_global = ProviderRegistryStore().path
        identifier = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:32]
        default_workspace = default_global.parent / "workspaces" / identifier / "providers.json"
        self._models = {
            "global": ModelPreferenceStore(default_global.with_name("config.json")),
            "workspace": ModelPreferenceStore(default_workspace.with_name("config.json")),
        }
        self.global_store = ProviderRegistryStore(paths.get("global", default_global))
        self.workspace_store = ProviderRegistryStore(
            paths.get("workspace", default_workspace),
            allow_external_active=True,
        )
        if self.global_store.path.resolve() == self.workspace_store.path.resolve():
            raise ValueError("Global and workspace providers must use different files")

    @property
    def path(self) -> Path:
        return self.global_store.path

    def store(self, scope: str) -> ProviderRegistryStore:
        if scope == "global":
            return self.global_store
        if scope == "workspace":
            return self.workspace_store
        raise ValueError("Provider scope must be global or workspace")

    def load_scope(self, scope: str) -> ProviderRegistry:
        return self.store(scope).load()

    def model_store(self, scope: str) -> ModelPreferenceStore:
        self.store(scope)
        return self._models[scope]

    def model(self) -> str:
        return self.model_store("workspace").load() or self.model_store("global").load()

    def load(self) -> ProviderRegistry:
        global_registry = self.global_store.load()
        local_registry = self.workspace_store.load()
        providers = {**global_registry.providers, **local_registry.providers}
        active = local_registry.active or global_registry.active
        if active and active not in providers:
            raise ValueError(f"Workspace selects missing provider connection {active!r}")
        return ProviderRegistry(local_registry.revision, active, providers)

    def snapshot(self, scope: str) -> dict[str, object]:
        global_registry = self.global_store.load()
        if scope == "global":
            return {
                **global_registry.snapshot(),
                "scope": scope,
                "path": str(self.global_store.path),
            }
        local_registry = self.workspace_store.load()
        combined = self.load()
        return {
            **combined.snapshot(),
            "scope": scope,
            "path": str(self.workspace_store.path),
            "global_active": global_registry.active,
            "inherited_active": not bool(local_registry.active),
            "origins": {
                name: "workspace" if name in local_registry.providers else "global" for name in combined.providers
            },
        }

    def save_scope(self, registry: ProviderRegistry, *, expected: str, scope: str) -> ProviderRegistry:
        if (
            scope == "workspace"
            and registry.active
            and registry.active not in registry.providers
            and registry.active not in self.global_store.load().providers
        ):
            raise ValueError("Selected connection does not exist in this workspace or globally")
        return self.store(scope).save(registry, expected=expected)
