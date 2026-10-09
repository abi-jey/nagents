"""Strict, secret-free JSON configuration.

Precedence: built-ins < NGN_* environment defaults < user JSON < trusted
project JSON < explicit JSON. Unknown mapping keys are errors.
Profiles accept ``mode`` (build/reviewer), ``instructions``, ``model`` and ``provider``.
Python extensions are executable trusted code, not sandboxed plugins.
"""

import json
import os
import re
import warnings
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path
from typing import Literal

from nagents.mcp import MCPServerConfig

from .providers import ProviderProfile
from .providers import ScopedProviderRegistryStore
from .providers import validate_provider
from .resource_config import parse_mcp_servers

THEME_NAMES: tuple[str, ...] = ("terminal", "graphite", "ocean", "ember")
DEFAULT_HARNESS_MODEL = "gpt-6-astra"
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
    provider: str = ""
    providers: dict[str, ProviderProfile] = field(default_factory=dict)
    model: str = DEFAULT_HARNESS_MODEL
    global_model_default: str = ""  # Loader-only baseline for global web defaults.
    agent: str = "assistant"
    read_only: bool = False
    plugins: tuple[str, ...] = ()
    mcp_servers: tuple[MCPServerConfig, ...] = ()
    resource_paths: tuple[Path, ...] = ()
    cli_plugins: tuple[str, ...] = ()
    trust_project: bool = False
    demo: bool = False
    data_dir: Path = field(default_factory=_data_dir)
    shell_timeout: float = 60.0
    max_output: int = 32768
    max_file_bytes: int = 262144
    max_tool_rounds: int = 30
    profiles: dict[str, AgentProfile] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    config_paths: tuple[Path, ...] = ()
    provider_paths: dict[str, Path] = field(default_factory=dict)
    theme: str = "terminal"
    animations: bool = True
    submit_mode: Literal["queue", "interrupt"] = "queue"
    tab_action: str = "agent"
    max_subagent_depth: int = 2
    theme_background: str = "auto"
    skill_token_limit: int = 10000
    # Distinguish a selected model from the built-in API default when switching to ChatGPT.
    model_explicit: bool = field(default=False, repr=False, compare=False)
    # Environment, trusted JSON, and CLI model choices outrank saved UI preferences at startup.
    model_config_explicit: bool = field(default=False, repr=False, compare=False)

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
        validate_provider(self.provider_profile())
        if not self.model.strip():
            raise ValueError("model must not be empty")
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

    def provider_profile(self) -> ProviderProfile:
        """Resolve the selected name; an unconfigured harness can still open setup."""
        if self.provider and self.provider not in self.providers:
            raise ValueError(f"Unknown provider connection {self.provider!r}")
        return self.providers.get(self.provider, ProviderProfile(kind="openai", auth="auto"))

    def profile(self, name: str) -> AgentProfile:
        if name == "assistant":
            return AgentProfile(mode="reviewer" if self.read_only else "build")
        if name not in self.profiles:
            raise ValueError(f"Unknown agent profile {name!r}; available: {', '.join(self.profile_names)}")
        return self.profiles[name]

    @property
    def profile_names(self) -> tuple[str, ...]:
        return ("assistant", *sorted(self.profiles))


def _provider_defaults(config: HarnessConfig, store: ScopedProviderRegistryStore) -> tuple[HarnessConfig, str]:
    """Select the active named provider without reading credential values."""
    registry = store.load()
    if registry.active:
        return (
            replace(
                config,
                provider=registry.active,
                providers=dict(registry.providers),
                model=store.model() or config.model,
                model_explicit=bool(store.model()) or config.model_explicit,
            ),
            f"Applied provider connection: {registry.active}",
        )
    return config, ""


def load_config(workspace: Path, config_path: Path | None = None, *, trust_project: bool = False) -> HarnessConfig:
    """Load trusted config only; never import plugins or read credential values."""
    defaults = HarnessConfig(workspace=workspace, trust_project=trust_project)
    directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ngn"
    user = Path(os.path.abspath((directory / "config.json").expanduser()))
    project = defaults.workspace / ".ngn/config.json"
    # Retain the operator-selected location, not a transient symlink target.
    # Kubernetes rotates ConfigMap ..data links without restarting the process.
    explicit = Path(os.path.abspath(config_path.expanduser())) if config_path is not None else None
    explicit_project = explicit is not None and explicit.resolve() == project.resolve()
    paths = [user]
    if project.exists() and not trust_project and not explicit_project:
        message = (
            f"Ignoring untrusted project config {project}; use trust_project=True to allow endpoints and Python code."
        )
        warnings.warn(message, UserWarning, stacklevel=2)
    if trust_project:
        paths.append(project)
    if explicit is not None:
        paths.append(explicit)
    documents: list[tuple[Path, dict[str, object]]] = []
    provider_paths: dict[str, Path] = {}
    for path in reversed(dict.fromkeys(reversed(paths))):
        if not path.exists():
            if path == explicit:
                raise FileNotFoundError(path)
            continue
        if path.suffix != ".json":
            raise ValueError(f"{path}: ngn configuration files must be .json")
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid configuration in {path}: {exc}") from exc
        if not isinstance(values, dict):
            raise ValueError(f"{path}: the configuration must be a mapping of top-level settings")
        if "providers" in values:
            references = values["providers"]
            if isinstance(references, str):
                references = {"global" if path.resolve() == user.resolve() else "workspace": references}
            if not isinstance(references, dict) or set(references) - {"global", "workspace"}:
                raise ValueError(f"{path}: providers must contain only global or workspace file references")
            for scope, reference in references.items():
                if not isinstance(reference, str) or not reference.strip():
                    raise ValueError(f"{path}: providers.{scope} must be a nonempty file path")
                target = Path(reference).expanduser()
                target = (target if target.is_absolute() else path.parent / target).resolve()
                if target.suffix != ".json" or target == path.resolve():
                    raise ValueError(f"{path}: providers.{scope} must refer to a separate .json file")
                provider_paths[scope] = target
        documents.append((path, values))
    store = ScopedProviderRegistryStore(defaults.workspace, paths=provider_paths)
    reserved = {path.resolve() for path, _ in documents}
    reserved.update(store.model_store(scope).path.resolve() for scope in ("global", "workspace"))
    if any(store.store(scope).path.resolve() in reserved for scope in ("global", "workspace")):
        raise ValueError("Provider registry cannot overwrite a config.json file")
    fallback_model = defaults.model
    config, login_note = _provider_defaults(defaults, store)
    selected_name = config.provider
    config.provider_paths = provider_paths
    config.providers = dict(store.load().providers)
    global_model = store.model_store("global").load() or fallback_model
    if os.environ.get("NGN_MODEL") is None:
        config.model = store.model() or config.model
    model_overridden = False
    strings = {
        "provider_id",
        "model",
        "agent",
        "theme",
        "submit_mode",
        "tab_action",
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
    allowed = (
        strings | integers | booleans | {"plugins", "data_dir", "shell_timeout", "profiles", "providers", "mcp_servers"}
    )
    for key in strings | integers | booleans | {"data_dir", "shell_timeout"}:
        env_value = os.environ.get(f"NGN_{key.upper()}")
        if env_value is None:
            continue
        if key == "provider_id":
            selected_name = env_value
        elif key in booleans:
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

    diagnostics: list[str] = [login_note] if login_note else []
    if project.exists() and not trust_project and not explicit_project:
        diagnostics.append(
            f"Ignoring untrusted project config {project}; use trust_project=True to allow endpoints and Python code."
        )
    loaded: list[Path] = []
    for path, values in documents:
        loaded.append(path)
        unknown = values.keys() - allowed
        if unknown:
            raise ValueError(
                f"Unknown configuration fields in {path}: {', '.join(sorted(unknown))}; configure connections in providers.json"
            )
        for key, value in values.items():
            if key == "providers":
                continue
            if key == "model" and isinstance(value, str):
                global_model = value
                if path != user or path == explicit:
                    model_overridden = True
                elif store.model_store("workspace").load():
                    continue
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
            elif key == "mcp_servers":
                value = parse_mcp_servers(value, path.parent, config.workspace)
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
            if key == "provider_id":
                assert isinstance(value, str)
                selected_name = value
            else:
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
    config.resource_paths = tuple(reversed(dict.fromkeys(paths[::-1])))
    config.global_model_default = global_model
    if selected_name:
        registry = store.load()
        if selected_name not in registry.providers:
            raise ValueError(f"Unknown provider connection {selected_name!r}")
        config.provider = selected_name
    else:
        config.provider = ""
    config.model_explicit = (
        config.model_explicit or model_overridden or bool(os.environ.get("NGN_MODEL") or store.model())
    )
    config.model_config_explicit = os.environ.get("NGN_MODEL") is not None or model_overridden
    config.__post_init__()
    return config
