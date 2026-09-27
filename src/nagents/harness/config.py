"""Strict, secret-free YAML configuration.

Precedence: built-ins < NGN_* environment defaults < user YAML < trusted
project YAML < explicit YAML. Unknown mapping keys are errors.
Profiles accept ``mode`` (build/reviewer), ``instructions``, ``model`` and ``provider``.
Python extensions are executable trusted code, not sandboxed plugins.
"""

import os
import re
import warnings
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml

from nagents.provider import ProviderType

from .credentials import ProviderLoginStore
from .private_store import ProtectedStoreError
from .providers import ScopedProviderRegistryStore

PROVIDERS = {provider.value: provider for provider in ProviderType}
PROVIDERS.update(
    openai=ProviderType.OPENAI_COMPATIBLE,
    gemini=ProviderType.GEMINI_NATIVE,
    google=ProviderType.GEMINI_NATIVE,
    azure=ProviderType.AZURE_OPENAI_COMPATIBLE,
    foundry=ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
)

THEME_NAMES: tuple[str, ...] = ("terminal", "graphite", "ocean", "ember")
DEFAULT_HARNESS_MODEL = "gpt-6-luna"
API_NAMES: tuple[str, ...] = ("auto", "chat_completions", "responses", "messages", "completions")
THEME_BACKGROUNDS: tuple[str, ...] = ("auto", "terminal", "theme")


def _data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "ngn"


@dataclass
class AgentProfile:
    mode: str = "build"
    instructions: str = ""
    model: str = ""
    provider: str = ""


@dataclass
class HarnessConfig:
    workspace: Path
    provider: str = "openai"
    provider_id: str = ""
    model: str = DEFAULT_HARNESS_MODEL
    global_model_default: str = ""  # Loader-only baseline for global web defaults.
    base_url: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    agent: str = "assistant"
    read_only: bool = False
    plugins: tuple[str, ...] = ()
    trust_project: bool = False
    demo: bool = False
    data_dir: Path = field(default_factory=_data_dir)
    api_version: str = ""
    shell_timeout: float = 60.0
    max_output: int = 32768
    max_file_bytes: int = 262144
    max_tool_rounds: int = 30
    profiles: dict[str, AgentProfile] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    config_paths: tuple[Path, ...] = ()
    auth: str = "auto"
    theme: str = "terminal"
    animations: bool = True
    submit_mode: Literal["queue", "interrupt"] = "queue"
    tab_action: str = "agent"
    api: str = "auto"
    max_subagent_depth: int = 2
    theme_background: str = "auto"
    skill_token_limit: int = 10000
    # Distinguish a selected model from the built-in API default when switching to ChatGPT.
    model_explicit: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.workspace = self.workspace.expanduser().resolve()
        self.data_dir = self.data_dir.expanduser().resolve()
        self.validate()

    def validate(self) -> None:
        """Re-run the trusted field checks; no I/O, plugins, or credential reads.

        Callers that overlay allowlisted fields on a copy can validate the
        candidate configuration without constructing a new dataclass.
        """
        if type(self.read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        if self.theme not in THEME_NAMES:
            raise ValueError(f"theme must be one of: {', '.join(THEME_NAMES)}")
        if self.theme_background not in THEME_BACKGROUNDS:
            raise ValueError(f"theme_background must be one of: {', '.join(THEME_BACKGROUNDS)}")
        if type(self.animations) is not bool:
            raise ValueError("animations must be a boolean")
        if self.submit_mode not in {"queue", "interrupt"}:
            raise ValueError("submit_mode must be queue or interrupt")
        if self.tab_action not in {"agent", "complete", "focus"}:
            raise ValueError("tab_action must be agent, complete, or focus")
        if self.provider not in PROVIDERS:
            raise ValueError(f"Unknown provider {self.provider!r}; choose from {', '.join(sorted(PROVIDERS))}")
        if self.api not in API_NAMES:
            raise ValueError(f"api must be one of: {', '.join(API_NAMES)}")
        if self.provider == "litellm" and not self.base_url:
            raise ValueError("LiteLLM requires an explicit base_url pointing to your gateway")
        if self.auth not in {"auto", "api-key", "chatgpt", "codex", "entra"}:
            raise ValueError("auth must be auto, api-key, chatgpt, codex, or entra")
        if self.auth == "entra" and self.provider not in {"foundry", "azure_openai_compatible_v1"}:
            raise ValueError("Entra authentication requires Foundry or Azure v1")
        if self.auth == "chatgpt" and (
            self.provider not in {"openai", "openai_compatible"} or self.base_url or self.api != "auto"
        ):
            raise ValueError(
                "ChatGPT login requires the default OpenAI provider endpoint, without base_url or api overrides"
            )
        if self.auth == "codex" and (self.provider != "openai" or self.base_url or self.api != "auto"):
            raise ValueError("Local Codex discovery requires the default OpenAI endpoint and API")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must be an environment variable name, not a literal secret")
        if self.base_url:
            url = urlsplit(self.base_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or any(char.isspace() or ord(char) < 32 for char in self.base_url)
            ):
                raise ValueError("base_url must be an HTTP(S) URL")
            if url.username or url.password or url.query or url.fragment:
                raise ValueError("base_url must not contain credentials, query parameters, or fragments")
        for name, profile in self.profiles.items():
            if name == "assistant":
                raise ValueError("The built-in assistant profile cannot be overridden")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or profile.mode not in {"build", "reviewer"}:
                raise ValueError(f"Invalid profile {name!r}: mode must be build or reviewer")
            if profile.provider and not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile.provider):
                raise ValueError(f"Invalid provider connection for profile {name!r}")
        self.profile(self.agent)
        if not 0 < self.shell_timeout <= 600:
            raise ValueError("shell_timeout must be > 0 and <= 600 seconds")
        if not 1024 <= self.max_output <= 1048576:
            raise ValueError("max_output must be between 1024 and 1048576 bytes")
        if not 1024 <= self.max_file_bytes <= 4194304:
            raise ValueError("max_file_bytes must be between 1024 and 4194304 bytes")
        if type(self.skill_token_limit) is not int or not 1 <= self.skill_token_limit <= 100000:
            raise ValueError("skill_token_limit must be an integer between 1 and 100000")
        if not 1 <= self.max_tool_rounds <= 1000:
            raise ValueError("max_tool_rounds must be between 1 and 1000")
        if type(self.max_subagent_depth) is not int or not 0 <= self.max_subagent_depth <= 8:
            raise ValueError("max_subagent_depth must be an integer between 0 and 8 (root depth is 0)")

    def profile(self, name: str) -> AgentProfile:
        if name == "assistant":
            return AgentProfile(mode="reviewer" if self.read_only else "build")
        if name not in self.profiles:
            raise ValueError(f"Unknown agent profile {name!r}; available: {', '.join(self.profile_names)}")
        return self.profiles[name]

    @property
    def profile_names(self) -> tuple[str, ...]:
        return ("assistant", *sorted(self.profiles))


def _login_defaults(config: HarnessConfig) -> tuple[HarnessConfig, str]:
    """Overlay a saved provider login below environment and file precedence.

    Only secret-free routing fields are read here. The API key remains in the
    protected store and is resolved lazily by the provider at request time.
    """
    store = ScopedProviderRegistryStore(config.workspace)
    registry = store.load()
    if registry.active:
        profile = registry.providers[registry.active]
        return (
            replace(
                config,
                provider_id=registry.active,
                provider=profile.kind,
                model=store.model() or config.model,
                model_explicit=bool(store.model()) or config.model_explicit,
                base_url=profile.base_url,
                api=profile.api,
                auth=profile.auth,
                api_key_env=profile.key_env,
                api_version=profile.api_version,
            ),
            f"Applied provider connection: {registry.active}",
        )
    try:
        selection = ProviderLoginStore().selection()
    except ProtectedStoreError:
        return config, "Ignored an unreadable saved provider login; run ngn login again."
    if selection is None:
        return config, ""
    if selection.model and not store.model():
        store.model_store("global").save(selection.model, only_if_missing=True)
    try:
        candidate = replace(
            config,
            provider=selection.provider,
            model=store.model() or config.model,
            model_explicit=bool(store.model()) or config.model_explicit,
            base_url=selection.base_url,
            api=selection.api or config.api,
            auth=selection.auth or config.auth,
            api_key_env=selection.api_key_env or config.api_key_env,
        )
    except ValueError:
        return config, "Ignored an unsupported saved provider login; run ngn login again."
    return candidate, f"Applied saved provider login: {candidate.provider} / {candidate.model}"


def load_config(workspace: Path, config_path: Path | None = None, *, trust_project: bool = False) -> HarnessConfig:
    """Load trusted config only; never import plugins or read credential values."""
    defaults = HarnessConfig(workspace=workspace, trust_project=trust_project)
    fallback_model = defaults.model
    config, login_note = _login_defaults(defaults)
    global_model = ScopedProviderRegistryStore(config.workspace).model_store("global").load() or fallback_model
    if os.environ.get("NGN_MODEL") is None:
        config.model = ScopedProviderRegistryStore(config.workspace).model() or config.model
    model_overridden = False
    strings = {
        "provider_id",
        "provider",
        "model",
        "base_url",
        "api_key_env",
        "agent",
        "api_version",
        "auth",
        "theme",
        "submit_mode",
        "tab_action",
        "api",
        "theme_background",
    }
    integers = {
        "max_output",
        "max_file_bytes",
        "skill_token_limit",
        "max_tool_rounds",
        "max_subagent_depth",
    }
    booleans = {"demo", "animations", "read_only"}
    allowed = strings | integers | booleans | {"plugins", "data_dir", "shell_timeout", "profiles"}
    for key in strings | integers | booleans | {"data_dir", "shell_timeout"}:
        env_value = os.environ.get(f"NGN_{key.upper()}")
        if env_value is None:
            continue
        if (
            key in {"provider", "base_url", "api", "auth", "api_key_env", "api_version"}
            and os.environ.get("NGN_PROVIDER_ID") is None
        ):
            config.provider_id = ""
        if key in booleans:
            if env_value.lower() not in {"true", "false", "1", "0"}:
                raise ValueError(f"NGN_{key.upper()} must be true/false or 1/0")
            setattr(config, key, env_value.lower() in {"true", "1"})
        elif key in integers:
            setattr(config, key, int(env_value))
        elif key == "shell_timeout":
            config.shell_timeout = float(env_value)
        else:
            setattr(config, key, Path(env_value) if key == "data_dir" else env_value)
            if key == "model":
                global_model = env_value

    user = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ngn/config.yaml"
    project = config.workspace / ".ngn/config.yaml"
    explicit = config_path.expanduser().resolve() if config_path is not None else None
    paths = [user]
    diagnostics: list[str] = [login_note] if login_note else []
    if project.exists() and not trust_project and explicit != project.resolve():
        message = (
            f"Ignoring untrusted project config {project}; use trust_project=True to allow endpoints and Python code."
        )
        warnings.warn(message, UserWarning, stacklevel=2)
        diagnostics.append(message)
    if trust_project:
        paths.append(project)
    if explicit is not None:
        paths.append(explicit)
    # Keep the last occurrence so explicitly selecting the global file still
    # overrides a trusted project file.
    loaded: list[Path] = []
    for path in reversed(dict.fromkeys(reversed(paths))):
        if not path.exists():
            if path == explicit:
                raise FileNotFoundError(path)
            continue
        loaded.append(path.resolve())
        try:
            values = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise ValueError(f"{path}: the configuration must be a mapping of top-level settings")
        unknown = values.keys() - allowed
        if unknown:
            raise ValueError(
                f"Unknown configuration fields in {path}: {', '.join(sorted(unknown))}; use api_key_env, never api_key"
            )
        if "provider_id" not in values and values.keys() & {
            "provider",
            "base_url",
            "api",
            "auth",
            "api_key_env",
            "api_version",
        }:
            config.provider_id = ""
        for key, value in values.items():
            if key == "model" and isinstance(value, str):
                global_model = value
                model_overridden = True
            if key in strings | {"data_dir"}:
                if not isinstance(value, str):
                    raise ValueError(f"{path}: {key} must be a string")
                if key == "data_dir":
                    value = Path(value).expanduser()
                    value = value if value.is_absolute() else path.parent / value
            elif key in integers:
                if type(value) is not int:
                    raise ValueError(f"{path}: {key} must be an integer")
            elif key == "shell_timeout":
                if type(value) not in {int, float}:
                    raise ValueError(f"{path}: shell_timeout must be a number")
            elif key in booleans:
                if type(value) is not bool:
                    raise ValueError(f"{path}: {key} must be a boolean")
            elif key == "plugins":
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError(f"{path}: plugins must be an array of 'path.py:setup' or 'module:setup' strings")
                plugins: list[str] = []
                for item in value:
                    module, separator, setup = item.rpartition(":")
                    if not separator or not setup.isidentifier() or not module:
                        raise ValueError(f"{path}: invalid plugin reference {item!r}")
                    if module.endswith(".py"):
                        plugin_path = Path(module).expanduser()
                        module = str((path.parent / plugin_path).resolve())
                    elif not all(part.isidentifier() for part in module.split(".")):
                        raise ValueError(f"{path}: invalid installed plugin module {module!r}")
                    plugins.append(f"{module}:{setup}")
                value = tuple(plugins)
            elif key == "profiles":
                if value is None:
                    value = {}
                if not isinstance(value, dict):
                    raise ValueError(f"{path}: profiles must be a mapping")
                for name, profile in value.items():
                    if not isinstance(profile, dict) or profile.keys() - {"mode", "instructions", "model", "provider"}:
                        raise ValueError(f"{path}: invalid profile {name!r}")
                    if not all(isinstance(item, str) for item in profile.values()):
                        raise ValueError(f"{path}: profile values must be strings")
                    config.profiles[name] = AgentProfile(**profile)
                continue
            setattr(config, key, value)
        diagnostics.append(f"Loaded trusted configuration: {path}")
    if config.agent in {"build", "agent", "reviewer"} and config.agent not in config.profiles:
        previous = config.agent
        config.agent = "assistant"
        config.read_only = config.read_only or previous == "reviewer"
        diagnostics.append(
            f"Migrated legacy agent selection {previous!r} to assistant"
            + (" in read-only mode" if config.read_only else "")
        )
    config.diagnostics = tuple(diagnostics)
    config.config_paths = tuple(loaded)
    config.global_model_default = global_model
    if config.provider_id:
        registry = ScopedProviderRegistryStore(config.workspace).load()
        if config.provider_id not in registry.providers:
            raise ValueError(f"Unknown provider connection {config.provider_id!r}")
        selected = registry.providers[config.provider_id]
        config.provider = selected.kind
        config.base_url = selected.base_url
        config.api = selected.api
        config.auth = selected.auth
        config.api_key_env = selected.key_env
        config.api_version = selected.api_version
    config.model_explicit = (
        config.model_explicit
        or model_overridden
        or bool(os.environ.get("NGN_MODEL") or ScopedProviderRegistryStore(config.workspace).model())
    )
    config.__post_init__()
    return config
