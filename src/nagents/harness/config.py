"""Strict, secret-free TOML configuration.

Precedence: built-ins < NGN_* environment defaults < user TOML < trusted
project TOML < explicit TOML. Tables other than ``[profiles.NAME]`` are errors.
Profiles accept ``mode`` (build/reviewer), ``instructions``, and ``model``.
Python extensions are executable trusted code, not sandboxed plugins.
"""

import os
import re
import tomllib
import warnings
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from urllib.parse import urlsplit

from nagents.provider import ProviderType

PROVIDERS = {provider.value: provider for provider in ProviderType}
PROVIDERS.update(
    openai=ProviderType.OPENAI_COMPATIBLE,
    gemini=ProviderType.GEMINI_NATIVE,
    google=ProviderType.GEMINI_NATIVE,
    azure=ProviderType.AZURE_OPENAI_COMPATIBLE,
)


def _data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "ngn"


@dataclass
class AgentProfile:
    mode: str = "build"
    instructions: str = ""
    model: str = ""


@dataclass
class HarnessConfig:
    workspace: Path
    provider: str = "openai"
    model: str = "gpt-4.1"
    base_url: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    agent: str = "build"
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

    def __post_init__(self) -> None:
        self.workspace = self.workspace.expanduser().resolve()
        self.data_dir = self.data_dir.expanduser().resolve()
        if self.provider not in PROVIDERS:
            raise ValueError(f"Unknown provider {self.provider!r}; choose from {', '.join(sorted(PROVIDERS))}")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must be an environment variable name, not a literal secret")
        if self.base_url:
            url = urlsplit(self.base_url)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("base_url must be an HTTP(S) URL")
            if url.username or url.password or url.query or url.fragment:
                raise ValueError("base_url must not contain credentials, query parameters, or fragments")
        for name, profile in self.profiles.items():
            if name in {"build", "reviewer"}:
                raise ValueError("Built-in build/reviewer profiles cannot be overridden")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or profile.mode not in {"build", "reviewer"}:
                raise ValueError(f"Invalid profile {name!r}: mode must be build or reviewer")
        self.profile(self.agent)
        if not 0 < self.shell_timeout <= 600:
            raise ValueError("shell_timeout must be > 0 and <= 600 seconds")
        if not 1024 <= self.max_output <= 1048576:
            raise ValueError("max_output must be between 1024 and 1048576 bytes")
        if not 1024 <= self.max_file_bytes <= 4194304:
            raise ValueError("max_file_bytes must be between 1024 and 4194304 bytes")
        if not 1 <= self.max_tool_rounds <= 1000:
            raise ValueError("max_tool_rounds must be between 1 and 1000")

    def profile(self, name: str) -> AgentProfile:
        if name in {"build", "reviewer"}:
            return AgentProfile(mode=name)
        if name not in self.profiles:
            raise ValueError(f"Unknown agent profile {name!r}; available: build, reviewer, {', '.join(self.profiles)}")
        return self.profiles[name]


def load_config(workspace: Path, config_path: Path | None = None, *, trust_project: bool = False) -> HarnessConfig:
    """Load trusted config only; never import plugins or read credential values."""
    config = HarnessConfig(workspace=workspace, trust_project=trust_project)
    strings = {"provider", "model", "base_url", "api_key_env", "agent", "api_version"}
    integers = {"max_output", "max_file_bytes", "max_tool_rounds"}
    allowed = strings | integers | {"plugins", "demo", "data_dir", "shell_timeout", "profiles"}
    for key in strings | {"data_dir", "demo", "shell_timeout"} | integers:
        env_value = os.environ.get(f"NGN_{key.upper()}")
        if env_value is None:
            continue
        if key == "demo":
            if env_value.lower() not in {"true", "false", "1", "0"}:
                raise ValueError("NGN_DEMO must be true/false or 1/0")
            setattr(config, key, env_value.lower() in {"true", "1"})
        elif key in integers:
            setattr(config, key, int(env_value))
        elif key == "shell_timeout":
            config.shell_timeout = float(env_value)
        else:
            setattr(config, key, Path(env_value) if key == "data_dir" else env_value)

    user = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "ngn/config.toml"
    project = config.workspace / ".ngn/config.toml"
    explicit = config_path.expanduser().resolve() if config_path is not None else None
    paths = [user]
    diagnostics: list[str] = []
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
    for path in reversed(dict.fromkeys(reversed(paths))):
        if not path.exists():
            if path == explicit:
                raise FileNotFoundError(path)
            continue
        try:
            with path.open("rb") as source:
                values = tomllib.load(source)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid TOML in {path}: {exc}") from exc
        unknown = values.keys() - allowed
        if unknown:
            raise ValueError(
                f"Unknown configuration fields in {path}: {', '.join(sorted(unknown))}; use api_key_env, never api_key"
            )
        for key, value in values.items():
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
            elif key == "demo":
                if type(value) is not bool:
                    raise ValueError(f"{path}: demo must be a boolean")
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
                if not isinstance(value, dict):
                    raise ValueError(f"{path}: profiles must be a table")
                for name, profile in value.items():
                    if not isinstance(profile, dict) or profile.keys() - {"mode", "instructions", "model"}:
                        raise ValueError(f"{path}: invalid profile {name!r}")
                    if not all(isinstance(item, str) for item in profile.values()):
                        raise ValueError(f"{path}: profile values must be strings")
                    config.profiles[name] = AgentProfile(**profile)
                continue
            setattr(config, key, value)
        diagnostics.append(f"Loaded trusted configuration: {path}")
    config.diagnostics = tuple(diagnostics)
    config.__post_init__()
    return config
