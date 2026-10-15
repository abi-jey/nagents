"""Strict, secret-free JSON configuration.

Precedence: built-ins < NGN_* environment defaults < user JSON < trusted
project JSON < explicit JSON. Unknown mapping keys are errors.
Profiles and imported manifests define instructions, routing, and permission limits.
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
from nagents.provider.reasoning import validate_reasoning_effort
from nagents.types import ReasoningEffort

from .agent_definitions import DefinitionBatch
from .agent_definitions import discover_agents
from .agent_definitions import import_agents
from .model_bindings import ModelBinding
from .providers import ProviderProfile
from .providers import ScopedProviderRegistryStore
from .providers import validate_provider
from .reasoning import ReasoningPreference
from .reasoning import resolve_effort
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
    description: str = ""
    tools: tuple[str, ...] | None = None
    denied_tools: tuple[str, ...] = ()
    allowed_agents: tuple[str, ...] | None = None
    user_invocable: bool = True
    subagent_available: bool = True
    source: str = field(default="", compare=False)
    source_format: str = field(default="", compare=False)
    reasoning_effort: ReasoningPreference = ""

    def validate(self) -> None:
        resolve_effort(self.reasoning_effort)
        if self.mode not in {"build", "reviewer"}:
            raise ValueError("mode must be build or reviewer")
        for name in ("instructions", "description", "model", "provider", "source", "source_format"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value.encode("utf-8")) > 262144:
                raise ValueError(f"{name} must be a bounded string")
        if self.provider and not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.provider):
            raise ValueError("Invalid provider connection")
        for name in ("user_invocable", "subagent_available"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        for name in ("tools", "denied_tools", "allowed_agents"):
            items = getattr(self, name)
            if items is None and name != "denied_tools":
                continue
            pattern = r"[A-Za-z0-9_-]{1,128}" if name == "allowed_agents" else r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}"
            if (
                not isinstance(items, tuple)
                or len(items) > 512
                or any(not isinstance(item, str) or not re.fullmatch(pattern, item) for item in items)
            ):
                raise ValueError(f"{name} must contain valid names")


@dataclass
class AgentDefinitionSource:
    """An operator-selected definition source, retained in precedence order."""

    kind: Literal["personal", "workspace", "configuration"]
    path: Path
    resolve: bool = False


@dataclass
class HarnessConfig:
    workspace: Path
    provider: str = ""
    providers: dict[str, ProviderProfile] = field(default_factory=dict)
    model: str = DEFAULT_HARNESS_MODEL
    # Runtime-only inheritance baseline. Effective agent routing must never
    # become the default when a host clones an already constructed harness.
    runtime_model_default: ModelBinding = field(default_factory=ModelBinding, repr=False, compare=False)
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
    agent_diagnostics: tuple[str, ...] = ()
    agent_sources: tuple[AgentDefinitionSource, ...] = ()
    agent_source_profiles: dict[str, AgentProfile] = field(default_factory=dict, repr=False, compare=False)
    config_paths: tuple[Path, ...] = ()
    # Explicit symlink configs historically resolve relative references beside
    # their target. Keep the logical location so extension reloads can rotate it.
    explicit_config_paths: tuple[Path, ...] = ()
    provider_paths: dict[str, Path] = field(default_factory=dict)
    theme: str = "terminal"
    animations: bool = True
    reasoning_summaries: bool = True
    reasoning_effort: ReasoningEffort = ""
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
        validate_reasoning_effort(self.reasoning_effort)
        if type(self.read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        if self.theme not in THEME_NAMES:
            raise ValueError(f"theme must be one of: {', '.join(THEME_NAMES)}")
        if self.theme_background not in THEME_BACKGROUNDS:
            raise ValueError(f"theme_background must be one of: {', '.join(THEME_BACKGROUNDS)}")
        if type(self.animations) is not bool:
            raise ValueError("animations must be a boolean")
        if type(self.reasoning_summaries) is not bool:
            raise ValueError("reasoning_summaries must be a boolean")
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
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) or profile.mode not in {"build", "reviewer"}:
                raise ValueError(f"Invalid profile {name!r}: mode must be build or reviewer")
            profile.validate()
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

    @property
    def selectable_profile_names(self) -> tuple[str, ...]:
        return tuple(name for name in self.profile_names if self.profile(name).user_invocable)


def _install_profile(config: HarnessConfig, name: str, profile: AgentProfile, diagnostics: list[str]) -> None:
    profile.validate()
    if name == "assistant":
        raise ValueError("The built-in assistant profile cannot be overridden")
    previous = config.profiles.get(name)
    if previous is not None:
        note = f"Agent {name!r}: {profile.source} overrides {previous.source or 'configured profile'}"
        diagnostics.append(note)
        config.agent_diagnostics += (note,)
    config.profiles[name] = profile


def _install_agents(config: HarnessConfig, batch: DefinitionBatch, diagnostics: list[str]) -> None:
    diagnostics.extend(batch.diagnostics)
    config.agent_diagnostics += batch.diagnostics
    for definition in batch.profiles:
        profile = AgentProfile(
            mode=definition.mode,
            instructions=definition.instructions,
            model=definition.model,
            provider=definition.provider,
            description=definition.description,
            tools=definition.tools,
            denied_tools=definition.denied_tools,
            allowed_agents=definition.allowed_agents,
            user_invocable=definition.user_invocable,
            subagent_available=definition.subagent_available,
            source=definition.source,
            source_format=definition.source_format,
            reasoning_effort=definition.reasoning_effort,
        )
        try:
            _install_profile(config, definition.name, profile, diagnostics)
        except ValueError as error:
            note = f"{definition.source}: {error}; agent was not loaded"
            diagnostics.append(note)
            config.agent_diagnostics += (note,)


def _json_profile(value: object, source: Path) -> AgentProfile:
    string_fields = {"mode", "instructions", "model", "provider", "description", "reasoning_effort"}
    bool_fields = {"user_invocable", "subagent_available"}
    list_fields = {"tools", "denied_tools", "allowed_agents"}
    if not isinstance(value, dict) or value.keys() - (string_fields | bool_fields | list_fields):
        raise ValueError(f"{source}: invalid agent profile fields")
    if isinstance(value.get("model"), dict):
        binding = ModelBinding.parse(value["model"])
        if "provider" in value and value["provider"] != binding.provider:
            raise ValueError(f"{source}: model provider conflicts with the separate provider field")
        value = {**value, "model": binding.id, "provider": binding.provider}
    profile = AgentProfile(source=str(source), source_format="json")
    for name, item in value.items():
        if name in string_fields:
            if not isinstance(item, str):
                raise ValueError(f"{source}: profile {name} must be a string")
        elif name in bool_fields:
            if type(item) is not bool:
                raise ValueError(f"{source}: profile {name} must be a boolean")
        else:
            if not isinstance(item, list) or not all(isinstance(entry, str) for entry in item):
                raise ValueError(f"{source}: profile {name} must be an array of names (omit it to inherit)")
            item = tuple(item)
        setattr(profile, name, item)
    profile.validate()
    return profile


def _provider_defaults(config: HarnessConfig, store: ScopedProviderRegistryStore) -> tuple[HarnessConfig, str]:
    """Select the active named provider without reading credential values."""
    registry = store.load()
    binding = store.model_binding("workspace")
    if not binding.configured:
        binding = store.model_binding("global", legacy_provider=registry.active)
    selected = binding.provider or registry.active
    if selected:
        if selected not in registry.providers:
            raise ValueError(f"Unknown provider connection {selected!r}")
        return (
            replace(
                config,
                provider=selected,
                providers=dict(registry.providers),
                model=store.model() or config.model,
                model_explicit=bool(store.model()) or config.model_explicit,
            ),
            f"Applied provider connection: {selected}",
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
    directories: dict[Path, Path] = {}
    provider_paths: dict[str, Path] = {}
    for path in reversed(dict.fromkeys(reversed(paths))):
        if not path.exists():
            if path == explicit:
                raise FileNotFoundError(path)
            continue
        if path.suffix != ".json":
            raise ValueError(f"{path}: ngn configuration files must be .json")
        source = path.resolve() if path == explicit else path
        directories[path] = source.parent
        try:
            values = json.loads(source.read_text(encoding="utf-8"))
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
                target = (target if target.is_absolute() else directories[path] / target).resolve()
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
        "reasoning_effort",
    }
    integers = {
        "max_output",
        "max_file_bytes",
        "skill_token_limit",
        "max_tool_rounds",
        "max_subagent_depth",
    }
    booleans = {"demo", "animations", "read_only", "reasoning_summaries"}
    allowed = (
        strings
        | integers
        | booleans
        | {"plugins", "data_dir", "shell_timeout", "profiles", "providers", "mcp_servers", "agent_imports"}
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
    agent_sources = [AgentDefinitionSource("personal", Path.home())]
    _install_agents(config, discover_agents(Path.home(), personal=True), diagnostics)
    project_agents_loaded = False
    if project.exists() and not trust_project and not explicit_project:
        diagnostics.append(
            f"Ignoring untrusted project config {project}; use trust_project=True to allow endpoints and Python code."
        )
    loaded: list[Path] = []
    for path, values in documents:
        if (path != user or path == explicit) and trust_project and not project_agents_loaded:
            agent_sources.append(AgentDefinitionSource("workspace", defaults.workspace))
            _install_agents(config, discover_agents(defaults.workspace), diagnostics)
            project_agents_loaded = True
        agent_sources.append(AgentDefinitionSource("configuration", path, path == explicit))
        loaded.append(path)
        unknown = values.keys() - allowed
        if unknown:
            raise ValueError(
                f"Unknown configuration fields in {path}: {', '.join(sorted(unknown))}; configure connections in providers.json"
            )
        if "agent_imports" in values:
            _install_agents(config, import_agents(values["agent_imports"], directories[path]), diagnostics)
        for key, value in values.items():
            if key in {"providers", "agent_imports"}:
                continue
            model_provider = ""
            if key == "model" and isinstance(value, dict):
                binding = ModelBinding.parse(value)
                value = binding.id
                model_provider = binding.provider
            if key == "model" and isinstance(value, str):
                global_model = value
                if path != user or path == explicit:
                    model_overridden = True
                elif store.model_store("workspace").load():
                    continue
                if model_provider:
                    selected_name = model_provider
            if key in strings | {"data_dir"}:
                if not isinstance(value, str):
                    raise ValueError(f"{path}: {key} must be a string")
                if key == "data_dir":
                    value = Path(value).expanduser()
                    value = value if value.is_absolute() else directories[path] / value
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
                value = parse_mcp_servers(value, directories[path], config.workspace)
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
                        module = str((directories[path] / plugin_path).resolve())
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
                    _install_profile(config, name, _json_profile(profile, path), diagnostics)
                continue
            if key == "provider_id":
                assert isinstance(value, str)
                if not isinstance(values.get("model"), dict):
                    selected_name = value
            else:
                setattr(config, key, value)
        diagnostics.append(f"Loaded trusted configuration: {path}")
    if trust_project and not project_agents_loaded:
        agent_sources.append(AgentDefinitionSource("workspace", defaults.workspace))
        _install_agents(config, discover_agents(defaults.workspace), diagnostics)
    # Keep absent trusted config locations too, so an operator can create one
    # later without restarting. Insert them at their original precedence.
    for path in paths:
        if not any(source.kind == "configuration" and source.path == path for source in agent_sources):
            definition_source = AgentDefinitionSource("configuration", path, path == explicit)
            if path == user and path != explicit:
                agent_sources.insert(1, definition_source)
            elif path == project:
                position = next(
                    (index + 1 for index, item in enumerate(agent_sources) if item.kind == "workspace"),
                    len(agent_sources),
                )
                agent_sources.insert(position, definition_source)
            else:
                agent_sources.append(definition_source)
    config.agent_sources = tuple(agent_sources)
    config.agent_source_profiles = {name: replace(profile) for name, profile in config.profiles.items()}
    config.diagnostics = tuple(diagnostics)
    config.config_paths = tuple(loaded)
    config.explicit_config_paths = (explicit,) if explicit is not None else ()
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
