"""Bounded, data-only imports of local agent manifests.

Discovery must only be called for trusted scopes. No adapter runs foreign hooks,
loads MCP servers, or grants permissions beyond the harness's own tool ceiling.
The native directory is scanned last, so callers can apply profiles in order.
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import cast

import yaml

from .live_profiles import LiveProfile
from .model_bindings import ModelBinding
from .reasoning import ReasoningPreference
from .reasoning import resolve_effort

MAX_AGENT_FILES = 256
MAX_AGENT_BYTES = 262144
MAX_HEADER_BYTES = 32768
MAX_NATIVE_HEADER_BYTES = 65536
MAX_ITEMS = 512
_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TOOL = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}\Z")
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_FORMATS = frozenset({"native", "claude", "copilot", "opencode", "codex", "cursor", "gemini", "antigravity"})


@dataclass(frozen=True)
class ParsedAgent:
    name: str
    description: str
    instructions: str
    mode: str = "build"
    model: str = ""
    provider: str = ""
    tools: tuple[str, ...] | None = None
    denied_tools: tuple[str, ...] = ()
    user_invocable: bool = True
    subagent_available: bool = True
    source: str = ""
    source_format: str = ""
    allowed_agents: tuple[str, ...] | None = None
    reasoning_effort: ReasoningPreference = ""
    live: LiveProfile = field(default_factory=LiveProfile)
    prompt: str = ""
    prompt_present: bool = False
    fields_present: tuple[str, ...] = ()
    auto_update: bool = False
    default_revision: str = ""
    field_sources: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DefinitionBatch:
    profiles: tuple[ParsedAgent, ...] = ()
    diagnostics: tuple[str, ...] = ()
    failed: bool = False


@dataclass(frozen=True)
class _Import:
    source_format: str
    model: str = ""
    provider: str = ""
    tool_map: tuple[tuple[str, tuple[str, ...]], ...] = ()


_FIELDS = {
    "native": {
        "name",
        "description",
        "model",
        "tools",
        "denied_tools",
        "mode",
        "user_invocable",
        "subagent_available",
        "allowed_agents",
        "reasoning_effort",
        "live",
        "prompt",
        "auto_update",
        "default_revision",
    },
    "claude": {"name", "description", "model", "tools", "disallowedTools", "permissionMode"},
    "copilot": {"name", "description", "model", "tools", "user-invocable", "disable-model-invocation", "agents"},
    "opencode": {"description", "model", "tools", "permission", "mode"},
    "codex": {"name", "description", "developer_instructions", "model", "sandbox_mode"},
    "cursor": {"name", "description", "model", "readonly", "is_background"},
    "gemini": {"name", "description", "model", "tools", "kind"},
    "antigravity": {"name", "description", "model", "tools", "mainAgent", "subagent"},
}

# These mappings describe permission groups, not implementations of foreign tools.
# Deliberately absent names (web, todo, MCP and extension tools) need an explicit
# trusted tool_map pointing at tools installed in this ngn workspace.
_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "claude": {
        "read": ("read_file",),
        "glob": ("find",),
        "grep": ("search",),
        "bash": ("shell",),
        "edit": ("edit",),
        "write": ("write",),
        "task": ("delegate",),
        "agent": ("delegate",),
        "skill": ("skill",),
    },
    "copilot": {
        "read": ("read_file", "list_files"),
        "read/readfile": ("read_file",),
        "read/listdirectory": ("list_files",),
        "search": ("search", "find"),
        "search/textsearch": ("search",),
        "search/filesearch": ("find",),
        "edit": ("edit", "write"),
        "edit/editfiles": ("edit", "write"),
        "execute": ("shell",),
        "execute/runinterminal": ("shell",),
        "shell": ("shell",),
        "bash": ("shell",),
        "agent": ("delegate",),
        "agent/runsubagent": ("delegate",),
    },
    "opencode": {
        "read": ("read_file",),
        "glob": ("find",),
        "grep": ("search",),
        "list": ("list_files",),
        "bash": ("shell",),
        "edit": ("edit", "write"),
        "write": ("write",),
        "task": ("delegate",),
        "skill": ("skill",),
    },
    "gemini": {
        "read_file": ("read_file",),
        "read_many_files": ("read_file",),
        "list_directory": ("list_files",),
        "glob": ("find",),
        "grep_search": ("search",),
        "search_file_content": ("search",),
        "run_shell_command": ("shell",),
        "replace": ("edit",),
        "write_file": ("write",),
    },
    "antigravity": {
        "view_file": ("read_file",),
        "list_dir": ("list_files",),
        "find_by_name": ("find",),
        "grep_search": ("search",),
        "run_command": ("shell",),
        "replace_file_content": ("edit",),
        "multi_replace_file_content": ("edit",),
        "write_to_file": ("write",),
    },
}


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be a mapping with string keys")
    return dict(value)


def _text(value: object, field: str, *, optional: bool = False, limit: int = 8192) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or (not optional and not value.strip()):
        raise ValueError(f"{field} must be {'a' if optional else 'a nonempty'} string of at most {limit} characters")
    return value.strip()


def _boolean(data: dict[str, object], field: str, default: bool) -> bool:
    value = data.get(field, default)
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _names(value: object, field: str, pattern: re.Pattern[str] = _TOOL) -> tuple[str, ...]:
    if isinstance(value, str):
        values: list[object] = [part.strip() for part in value.split(",")] if value.strip() else []
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError(f"{field} must be a list of names or a comma-separated string")
    if len(values) > MAX_ITEMS:
        raise ValueError(f"{field} has too many entries (limit {MAX_ITEMS})")
    result: list[str] = []
    for name in values:
        if not isinstance(name, str) or not pattern.fullmatch(name):
            raise ValueError(f"{field} contains an invalid name: {name!r}")
        if name not in result:
            result.append(name)
    return tuple(result)


def _string_names(value: object, field: str) -> tuple[str, ...]:
    # Foreign tool names can contain '/', and are validated by the mapping below.
    return _names(value, field, re.compile(r"[A-Za-z_*][A-Za-z0-9_./:*-]{0,127}\Z"))


def _tools(value: object, field: str, options: _Import) -> tuple[str, ...]:
    if options.source_format == "native":
        return _names(value, field)
    overrides = dict(options.tool_map)
    aliases = _ALIASES.get(options.source_format, {})
    result: list[str] = []
    for name in _string_names(value, field):
        mapped = overrides.get(name, aliases.get(name.casefold()))
        if mapped is None:
            raise ValueError(
                f"{field}: unsupported {options.source_format} tool {name!r}; configure agent_imports tool_map"
            )
        result.extend(item for item in mapped if item not in result)
    return tuple(result)


def _tool_policy(data: dict[str, object], options: _Import) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    fmt = options.source_format
    denied: tuple[str, ...] = ()
    allowed: tuple[str, ...] | None = () if fmt == "antigravity" else None
    if fmt == "opencode":
        if "tools" in data:
            choices = _mapping(data["tools"], "tools")
            for name, value in choices.items():
                if type(value) is not bool:
                    raise ValueError(f"tools.{name} must be a boolean")
                mapped = _tools([name], "tools", options)
                if not value:
                    denied += mapped
        if "permission" in data:
            for name, action in _mapping(data["permission"], "permission").items():
                mapped = _tools([name], "permission", options)
                if not isinstance(action, str) or action not in {"allow", "ask", "deny"}:
                    raise ValueError(f"permission.{name} must be allow, ask or deny; pattern policies are unsupported")
                if action == "ask":
                    raise ValueError(
                        f"permission.{name}: ask requires per-call approval that ngn cannot preserve; use a representable restriction such as deny"
                    )
                if action == "deny":
                    denied += mapped
        return allowed, tuple(dict.fromkeys(denied))
    if "tools" in data:
        if fmt == "copilot" and _string_names(data["tools"], "tools") == ("*",):
            allowed = None
        else:
            allowed = _tools(data["tools"], "tools", options)
    deny_field = "disallowedTools" if fmt == "claude" else "denied_tools"
    if deny_field in data:
        denied = _tools(data[deny_field], deny_field, options)
    return allowed, denied


def _yaml_header(source: str, *, max_bytes: int = MAX_HEADER_BYTES) -> dict[str, object]:
    # Discovery still validates each path and rereads its bytes on every round.
    # Only unchanged parsing is reused; each caller owns its nested policy data.
    return deepcopy(_parsed_yaml_header(source, max_bytes))


@lru_cache(maxsize=128)
def _parsed_yaml_header(source: str, max_bytes: int) -> dict[str, object]:
    if len(source.encode("utf-8")) > max_bytes:
        raise ValueError(f"YAML frontmatter exceeds {max_bytes // 1024} KiB")
    for index, token in enumerate(yaml.scan(source)):
        if index > 4096:
            raise ValueError("YAML frontmatter has too many tokens")
        if isinstance(token, yaml.tokens.AliasToken | yaml.tokens.AnchorToken | yaml.tokens.TagToken):
            raise ValueError("YAML aliases, anchors and explicit tags are unsupported")
    nesting = 0
    for event in yaml.parse(source, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.events.CollectionStartEvent):
            nesting += 1
            if nesting > 16:
                raise ValueError("YAML frontmatter is too deeply nested")
        elif isinstance(event, yaml.events.CollectionEndEvent):
            nesting -= 1

    def unique(node: yaml.Node, depth: int = 0) -> None:
        if depth > 16:
            raise ValueError("YAML frontmatter is too deeply nested")
        if isinstance(node, yaml.MappingNode):
            seen: set[str] = set()
            for key, value in node.value:
                if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in seen:
                    raise ValueError("YAML mapping keys must be unique strings")
                seen.add(key.value)
                unique(value, depth + 1)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                unique(value, depth + 1)

    document = yaml.compose(source, Loader=yaml.SafeLoader)
    if document is not None:
        unique(document)
    parsed: object = yaml.safe_load(source)
    return _mapping(parsed, "YAML frontmatter")


def _markdown(source: str, fmt: str) -> tuple[dict[str, object], str]:
    lines = source.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        if fmt in {"cursor", "copilot"}:
            return {}, source.strip()
        raise ValueError("expected YAML frontmatter beginning with ---")
    for index, line in enumerate(lines[1:], 1):
        if line.rstrip() == "---":
            header = "".join(lines[1:index])
            data = (
                {}
                if fmt == "native" and not header.strip()
                else _yaml_header(header, max_bytes=MAX_NATIVE_HEADER_BYTES if fmt == "native" else MAX_HEADER_BYTES)
            )
            return data, "".join(lines[index + 1 :]).strip()
    raise ValueError("YAML frontmatter is missing its closing --- delimiter")


def _checked_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("agent sources must not use symlinks")
    return path


def _read(path: Path) -> str:
    path = _checked_path(path)
    try:
        metadata = path.stat()
    except FileNotFoundError:
        raise ValueError("agent source file does not exist") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("agent source must be a regular file")
    if metadata.st_size > MAX_AGENT_BYTES:
        raise ValueError("agent source exceeds 256 KiB")
    with path.open("rb") as stream:
        raw = stream.read(MAX_AGENT_BYTES + 1)
    if len(raw) > MAX_AGENT_BYTES:
        raise ValueError("agent source exceeds 256 KiB")
    return raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def _parse(path: Path, options: _Import) -> ParsedAgent:
    source = _read(path)
    fmt = options.source_format
    if fmt == "codex":
        try:
            data = _mapping(tomllib.loads(source), "Codex TOML")
        except RecursionError:
            raise ValueError("Codex TOML is too deeply nested") from None
        body = _text(data.get("developer_instructions"), "developer_instructions", limit=MAX_AGENT_BYTES)
    else:
        data, body = _markdown(source, fmt)
    if fmt == "native" and body:
        raise ValueError("Native agent files must contain only frontmatter; put instructions in prompt: |")
    unknown = sorted(set(data) - _FIELDS[fmt])
    if unknown:
        raise ValueError(
            f"unsupported {fmt} field(s): {', '.join(unknown)}; hooks, MCP and foreign runtime settings are not imported"
        )
    fallback = path.parent.name if path.name == "agent.md" else path.stem.removesuffix(".agent")
    require_name = fmt in {"claude", "codex", "gemini", "antigravity"}
    name = _text(data.get("name", "" if require_name else fallback), "name", limit=128)
    if not _NAME.fullmatch(name) or (name == "assistant" and fmt != "native"):
        raise ValueError("name must use letters, numbers, underscores or hyphens and must not replace assistant")
    description = _text(data.get("description", ""), "description", optional=fmt in {"native", "cursor", "copilot"})
    body = _text(body, "instructions", optional=fmt == "native", limit=MAX_AGENT_BYTES)
    prompt = ""
    prompt_present = fmt == "native" and "prompt" in data
    auto_update = False
    default_revision = ""
    fields_present: set[str] = set()
    if fmt == "native":
        fields_present.update(set(data) - {"name"})
        if prompt_present:
            prompt_value = data["prompt"]
            _text(prompt_value, "prompt", optional=True, limit=24000)
            assert isinstance(prompt_value, str)
            try:
                if len(prompt_value.encode("utf-8")) > 48000:
                    raise ValueError("prompt exceeds the UTF-8 size limit")
            except UnicodeError:
                raise ValueError("prompt must contain valid Unicode text") from None
            prompt = prompt_value
            fields_present.add("prompt_present")
        auto_update = _boolean(data, "auto_update", False)
        default_revision_value = data.get("default_revision", "")
        if not isinstance(default_revision_value, str) or (
            "default_revision" in data and not re.fullmatch(r"[0-9a-f]{64}", default_revision_value)
        ):
            raise ValueError("default_revision must be a SHA-256 hexadecimal digest")
        default_revision = default_revision_value
    raw_model = data.get("model", "")
    if fmt == "native":
        binding = ModelBinding.parse(raw_model) if "model" in data else ModelBinding()
        model, provider = binding.id, binding.provider
        if "model" in data:
            fields_present.add("provider")
    else:
        model = _text(raw_model, "model", optional=True, limit=256)
        provider = _text(data.get("provider", ""), "provider", optional=True, limit=64)
    if fmt != "native":
        if model and model != "inherit" and not options.model:
            raise ValueError(
                f"model {model!r} is specific to {fmt}; configure agent_imports model (and provider when needed)"
            )
        model, provider = options.model, options.provider
    else:
        model, provider = options.model or model, options.provider or provider
        if options.model:
            fields_present.add("model")
        if options.provider:
            fields_present.add("provider")
    if provider and not _PROVIDER.fullmatch(provider):
        raise ValueError("provider must name an ngn provider connection")
    effort = _text(data.get("reasoning_effort", ""), "reasoning_effort", optional=True)
    resolve_effort(effort)
    allowed, denied = _tool_policy(data, options)
    mode = "build"
    user_invocable = True
    subagent_available = True
    allowed_agents: tuple[str, ...] | None = None
    if fmt == "native":
        mode = _text(data.get("mode", "build"), "mode")
        if mode not in {"build", "reviewer"}:
            raise ValueError("mode must be build or reviewer")
        user_invocable = _boolean(data, "user_invocable", True)
        subagent_available = _boolean(data, "subagent_available", True)
        if "allowed_agents" in data:
            allowed_agents = _names(data["allowed_agents"], "allowed_agents", _NAME)
    elif fmt == "claude":
        permission = data.get("permissionMode", "default")
        if permission not in ("default", "plan"):
            raise ValueError("permissionMode supports only default or plan; ngn retains its approval rules")
        if permission == "plan":
            mode = "reviewer"
    elif fmt == "copilot":
        user_invocable = _boolean(data, "user-invocable", True)
        subagent_available = not _boolean(data, "disable-model-invocation", False)
        if "agents" in data:
            targets = _string_names(data["agents"], "agents")
            allowed_agents = None if targets == ("*",) else _names(data["agents"], "agents", _NAME)
            if allowed_agents != () and allowed is not None and "delegate" not in allowed:
                raise ValueError("agents requires the agent tool in tools")
    elif fmt == "opencode":
        foreign_mode = data.get("mode", "all")
        if foreign_mode not in ("primary", "subagent", "all"):
            raise ValueError("mode must be primary, subagent or all")
        user_invocable = foreign_mode != "subagent"
        subagent_available = foreign_mode != "primary"
    elif fmt == "codex" and "sandbox_mode" in data:
        if data["sandbox_mode"] != "read-only":
            raise ValueError("sandbox_mode supports only read-only; ngn cannot reproduce foreign shell sandboxes")
        mode = "reviewer"
    elif fmt == "cursor":
        if _boolean(data, "readonly", False):
            mode = "reviewer"
        if _boolean(data, "is_background", False):
            raise ValueError("is_background scheduling is unsupported; use ngn delegation")
    elif fmt == "gemini":
        if data.get("kind", "local") != "local":
            raise ValueError("kind must be local; remote agents are unsupported")
        allowed_agents = ()
    elif fmt == "antigravity":
        user_invocable = _boolean(data, "mainAgent", True)
        subagent_available = _boolean(data, "subagent", True)
    live = LiveProfile.parse(data.get("live", {}))
    source_fields = (
        fields_present
        if fmt == "native"
        else set(ParsedAgent.__dataclass_fields__)
        - {"name", "source", "source_format", "fields_present", "field_sources"}
    )
    field_sources = {field_name: str(path.absolute()) for field_name in source_fields}
    field_sources.update({f"live.{field_name}": str(path.absolute()) for field_name in live.selected()})
    return ParsedAgent(
        name,
        description,
        body,
        mode,
        model,
        provider,
        allowed,
        denied,
        user_invocable,
        subagent_available,
        str(path.absolute()),
        fmt,
        allowed_agents,
        cast("ReasoningPreference", effort),
        live,
        prompt,
        prompt_present,
        tuple(sorted(fields_present)),
        auto_update,
        default_revision,
        tuple(sorted(field_sources.items())),
    )


def _merge_native_profile(previous: ParsedAgent, profile: ParsedAgent) -> ParsedAgent:
    """Merge native fragments before discovery collapses a same-name source."""
    changes = {name: getattr(profile, name) for name in profile.fields_present}
    if "live" in changes:
        changes["live"] = LiveProfile.parse({**previous.live.selected(), **profile.live.selected()})
    previous_fields = (
        set(previous.fields_present)
        if previous.source_format == "native"
        else set(previous.__dataclass_fields__) - {"name", "source", "source_format", "fields_present", "field_sources"}
    )
    previous_sources = {name: previous.source for name in previous_fields}
    previous_sources.update({f"live.{name}": previous.source for name in previous.live.selected()})
    previous_sources.update(dict(previous.field_sources))
    previous_sources.update(dict(profile.field_sources))
    return replace(
        previous,
        **changes,
        source=profile.source,
        source_format=profile.source_format,
        fields_present=tuple(sorted(previous_fields | set(profile.fields_present))),
        field_sources=tuple(sorted(previous_sources.items())),
    )


def _roots(base: Path, personal: bool) -> tuple[tuple[Path, str], ...]:
    roots: list[tuple[Path, str]] = [
        (base / ".claude/agents", "claude"),
        (base / (".copilot/agents" if personal else ".github/agents"), "copilot"),
        (base / ".codex/agents", "codex"),
        (base / ".cursor/agents", "cursor"),
        (base / ".gemini/agents", "gemini"),
    ]
    if personal:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or base / ".config")
        if not config.is_absolute():
            config = base / ".config"
        roots.extend(((config / "opencode/agents", "opencode"), (base / ".gemini/config/agents", "antigravity")))
        roots.append((config / "ngn/agents", "native"))
    else:
        roots.insert(0, (base / ".agents/agents", "antigravity"))
        roots.insert(3, (base / ".opencode/agents", "opencode"))
        roots.append((base / ".ngn/agents", "native"))
    return tuple(roots)


def _paths(root: Path, fmt: str, diagnostics: list[str]) -> list[Path]:
    if not root.exists() and not root.is_symlink():
        return []
    try:
        _checked_path(root)
        if not root.is_dir():
            raise ValueError("agent discovery root must be a directory")
    except (OSError, ValueError) as exc:
        diagnostics.append(f"{root}: {exc}")
        return []
    paths: list[Path] = []
    suffix = ".toml" if fmt == "codex" else ".md"
    # Only documented top-level files and <name>/agent.md packages are scanned.
    # Arbitrary directory trees and foreign configuration files are never loaded.
    try:
        entries: list[Path] = []
        with os.scandir(root) as scan:
            for scanned_entry in scan:
                if len(entries) >= MAX_AGENT_FILES * 4:
                    raise ValueError(f"agent discovery directory exceeds {MAX_AGENT_FILES * 4} entries")
                entries.append(Path(scanned_entry.path))
        for entry in sorted(entries):
            if entry.is_symlink():
                diagnostics.append(f"{entry}: agent sources must not use symlinks")
                continue
            if entry.is_file() and entry.suffix == suffix:
                paths.append(entry)
            elif fmt in {"native", "antigravity"} and entry.is_dir():
                nested = entry / "agent.md"
                if nested.exists() or nested.is_symlink():
                    paths.append(nested)
            if len(paths) > MAX_AGENT_FILES:
                break
    except (OSError, ValueError) as exc:
        diagnostics.append(f"{root}: {exc}")
    return paths


def discover_agents(base: Path, *, personal: bool = False) -> DefinitionBatch:
    """Discover a trusted workspace or home, returning source-ordered profiles.

    Later profiles with the same name win. Collisions are diagnosed here so a
    caller may retain the winning source without silently hiding another file.
    """
    profiles: dict[str, ParsedAgent] = {}
    diagnostics: list[str] = []
    scanned = 0
    failed = False
    for root, fmt in _roots(base.absolute(), personal):
        before = len(diagnostics)
        paths = _paths(root, fmt, diagnostics)
        failed = failed or len(diagnostics) > before
        for path in paths:
            scanned += 1
            if scanned > MAX_AGENT_FILES:
                diagnostics.append(
                    f"{root}: agent discovery exceeded {MAX_AGENT_FILES} files; remaining sources skipped"
                )
                return DefinitionBatch(tuple(profiles.values()), tuple(diagnostics), True)
            try:
                profile = _parse(path, _Import(fmt))
            except (OSError, ValueError, yaml.YAMLError) as exc:
                failed = True
                diagnostics.append(f"{path}: {exc}")
                continue
            if profile.name in profiles:
                diagnostics.append(f"{profile.source}: agent {profile.name!r} replaces {profiles[profile.name].source}")
                if profile.source_format == "native" and profiles[profile.name].source_format == "native":
                    profile = _merge_native_profile(profiles[profile.name], profile)
                del profiles[profile.name]
            profiles[profile.name] = profile
    return DefinitionBatch(tuple(profiles.values()), tuple(diagnostics), failed)


def import_agents(value: object, directory: Path) -> DefinitionBatch:
    """Import explicitly selected files or definition directories.

    Absolute paths and ../ selections are intentional trusted choices. Relative
    paths resolve beside the configuration; neither imports nor discovery follow
    symlinks. Invalid entries produce diagnostics without executing anything.
    """
    if not isinstance(value, list) or len(value) > MAX_AGENT_FILES:
        return DefinitionBatch(
            diagnostics=(f"agent_imports must be a list of at most {MAX_AGENT_FILES} imports",), failed=True
        )
    profiles: list[ParsedAgent] = []
    diagnostics: list[str] = []
    scanned = 0
    for index, item in enumerate(value):
        label = f"agent_imports[{index}]"
        try:
            data = _mapping(item, label)
            unknown = sorted(set(data) - {"path", "format", "model", "provider", "tool_map"})
            if unknown:
                raise ValueError(f"unsupported import field(s): {', '.join(unknown)}")
            raw_path = _text(data.get("path"), "path", limit=4096)
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = directory / path
            label = str(path.absolute())
            fmt = _text(data.get("format"), "format", limit=32)
            if fmt not in _FORMATS:
                raise ValueError(f"format must be one of: {', '.join(sorted(_FORMATS))}")
            model = _text(data.get("model", ""), "model override", optional=True, limit=256)
            provider = _text(data.get("provider", ""), "provider override", optional=True, limit=64)
            if "model" in data and not model:
                raise ValueError("model override must be nonempty when provided")
            tool_map = tuple(
                (name, _names(targets, f"tool_map.{name}"))
                for name, targets in _mapping(data.get("tool_map", {}), "tool_map").items()
            )
            if len(tool_map) > MAX_ITEMS:
                raise ValueError(f"tool_map has too many entries (limit {MAX_ITEMS})")
            for name, targets in tool_map:
                if not name or len(name) > 128 or not targets:
                    raise ValueError("tool_map must map nonempty tool names to nonempty lists of ngn tool names")
            paths = _paths(path, fmt, diagnostics) if path.is_dir() else [path]
            options = _Import(fmt, model, provider, tool_map)
            for source in paths:
                scanned += 1
                if scanned > MAX_AGENT_FILES:
                    diagnostics.append(
                        f"{source}: agent imports exceeded {MAX_AGENT_FILES} files; remaining sources skipped"
                    )
                    return DefinitionBatch(tuple(profiles), tuple(diagnostics), bool(diagnostics))
                try:
                    profiles.append(_parse(source, options))
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    diagnostics.append(f"{source}: {exc}")
        except (OSError, ValueError, yaml.YAMLError) as exc:
            diagnostics.append(f"{label}: {exc}")
    return DefinitionBatch(tuple(profiles), tuple(diagnostics), bool(diagnostics))
