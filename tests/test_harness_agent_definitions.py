"""Agent adapters retain source restrictions without loading foreign runtimes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.harness import agent_definitions
from nagents.harness.agent_definitions import discover_agents
from nagents.harness.agent_definitions import import_agents

if TYPE_CHECKING:
    from pathlib import Path


def _write(base: Path, name: str, source: str) -> Path:
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _markdown(header: str, body: str = "Inspect the project and explain your findings.") -> str:
    return f"---\n{header}\n---\n{body}\n"


def test_native_multiline_body_metadata_and_tool_groups(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        ".ngn/agents/research.md",
        _markdown(
            "description: |\n  Review the architecture.\n  Explain the evidence.\n"
            "model: gpt-6-luna\nprovider: work-login\nmode: reviewer\n"
            "tools: read_file, search, custom:lookup\ndenied_tools: [shell]\n"
            "user_invocable: false\nsubagent_available: true\nallowed_agents: []",
            "# Instructions\n\nKeep **Markdown** formatting.\n\n---\n\nThis rule belongs to the body.",
        ),
    )
    batch = discover_agents(tmp_path)
    assert batch.diagnostics == ()
    (profile,) = batch.profiles
    assert profile.name == "research"
    assert profile.description == "Review the architecture.\nExplain the evidence."
    assert "**Markdown**" in profile.instructions
    assert "---\n\nThis rule belongs" in profile.instructions
    assert (profile.model, profile.provider, profile.mode) == ("gpt-6-luna", "work-login", "reviewer")
    assert profile.tools == ("read_file", "search", "custom:lookup")
    assert profile.denied_tools == ("shell",)
    assert not profile.user_invocable
    assert profile.subagent_available
    assert profile.allowed_agents == ()
    assert (profile.source, profile.source_format) == (str(path), "native")


@pytest.mark.parametrize("field, expected", [("", None), ("\ntools: []", ()), ("\ntools: ''", ())])
def test_native_omitted_and_explicit_empty_tools_differ(tmp_path: Path, field: str, expected: object) -> None:
    _write(tmp_path, ".ngn/agents/scan.md", _markdown("description: Scan safely" + field))
    batch = discover_agents(tmp_path)
    assert batch.diagnostics == ()
    assert batch.profiles[0].tools == expected


@pytest.mark.parametrize(
    "header, message",
    [
        ("tools: [read_file]", "description"),
        ("description: Scan\nname: assistant", "must not replace assistant"),
        ("description: Scan\nname: bad/name", "name must use"),
        ("description: Scan\nmode: admin", "mode must be"),
        ("description: Scan\nuser_invocable: 1", "must be a boolean"),
        ("description: Scan\nsubagent_available: null", "must be a boolean"),
        ("description: Scan\ntools: null", "must be a list"),
        ("description: Scan\ntools: ['*']", "invalid name"),
        ("description: Scan\nprovider: OpenAI", "provider must name"),
        ("description: Scan\npermission: allow", "unsupported native field"),
        ("description: Scan\ndescription: Duplicate", "unique strings"),
        ("description: Scan\ntools: &tools [read_file]", "aliases, anchors"),
        ("description: Scan\ntools: *tools", "aliases, anchors"),
        ("description: !!str Scan", "explicit tags"),
        ("description: Scan\n1: invalid", "unique strings"),
        ("description: Scan\n...\nname: another_document", "expected"),
    ],
)
def test_invalid_native_data_is_diagnosed_with_source(tmp_path: Path, header: str, message: str) -> None:
    path = _write(tmp_path, ".ngn/agents/bad.md", _markdown(header))
    batch = discover_agents(tmp_path)
    assert not batch.profiles
    assert len(batch.diagnostics) == 1
    assert str(path) in batch.diagnostics[0]
    assert message in batch.diagnostics[0]


def test_nested_yaml_is_rejected_before_recursive_composition(tmp_path: Path) -> None:
    _write(tmp_path, ".ngn/agents/deep.md", _markdown("description: Deep\ntools: " + "[" * 600 + "]" * 600))
    batch = discover_agents(tmp_path)
    assert not batch.profiles
    assert "too deeply nested" in batch.diagnostics[0]


def test_deep_toml_does_not_abort_loading_other_agents(tmp_path: Path) -> None:
    path = _write(tmp_path, ".codex/agents/deep.toml", "tools = " + "[" * 2000 + "0" + "]" * 2000)
    _write(tmp_path, ".ngn/agents/valid.md", _markdown("description: Still available"))
    batch = discover_agents(tmp_path)
    assert [profile.name for profile in batch.profiles] == ["valid"]
    assert any("too deeply nested" in diagnostic for diagnostic in batch.diagnostics)
    imported = import_agents([{"path": str(path), "format": "codex"}], tmp_path)
    assert not imported.profiles
    assert "too deeply nested" in imported.diagnostics[0]


@pytest.mark.parametrize(
    "source, message", [("No frontmatter", "expected YAML"), ("---\ndescription: text", "closing ---")]
)
def test_malformed_markdown_reports_error(tmp_path: Path, source: str, message: str) -> None:
    _write(tmp_path, ".ngn/agents/bad.md", source)
    assert message in discover_agents(tmp_path).diagnostics[0]


def test_discovery_order_and_collision_winner_are_deterministic(tmp_path: Path) -> None:
    _write(tmp_path, ".claude/agents/b.md", _markdown("name: same\ndescription: Foreign"))
    _write(tmp_path, ".ngn/agents/z.md", _markdown("name: same\ndescription: Native winner"))
    _write(tmp_path, ".ngn/agents/a.md", _markdown("description: Alphabetical first"))
    _write(tmp_path, ".ngn/agents/m.md", _markdown("name: same\ndescription: Earlier native"))
    first = discover_agents(tmp_path)
    assert first == discover_agents(tmp_path)
    assert [profile.name for profile in first.profiles] == ["a", "same"]
    assert first.profiles[-1].description == "Native winner"
    assert len(first.diagnostics) == 2
    assert "replaces" in first.diagnostics[-1]
    assert ".ngn/agents/m.md" in first.diagnostics[-1]


@pytest.mark.parametrize(
    "filename, header, expected_tools, expected_denials, mode",
    [
        (
            ".claude/agents/reader.md",
            "name: reader\ndescription: Read\ntools: Read, Glob, Grep\ndisallowedTools: Bash\npermissionMode: plan",
            ("read_file", "find", "search"),
            ("shell",),
            "reviewer",
        ),
        (
            ".github/agents/reader.agent.md",
            "description: Read\ntools: [read, search, agent]\nagents: [reviewer]\nuser-invocable: false",
            ("read_file", "list_files", "search", "find", "delegate"),
            (),
            "build",
        ),
        (
            ".opencode/agents/reader.md",
            "description: Read\nmode: subagent\ntools:\n  bash: false\n  read: true\npermission:\n  edit: deny\n  task: allow",
            None,
            ("shell", "edit", "write"),
            "build",
        ),
        (".cursor/agents/reader.md", "readonly: true\nmodel: inherit", None, (), "reviewer"),
        (
            ".gemini/agents/reader.md",
            "name: reader\ndescription: Read\nkind: local\ntools: [read_file, glob, grep_search]",
            ("read_file", "find", "search"),
            (),
            "build",
        ),
        (
            ".agents/agents/reader.md",
            "name: reader\ndescription: Read\ntools: [view_file, grep_search]\nmainAgent: false",
            ("read_file", "search"),
            (),
            "build",
        ),
    ],
)
def test_foreign_safe_subsets_normalize_without_permission_grants(
    tmp_path: Path,
    filename: str,
    header: str,
    expected_tools: object,
    expected_denials: tuple[str, ...],
    mode: str,
) -> None:
    _write(tmp_path, filename, _markdown(header))
    batch = discover_agents(tmp_path)
    assert batch.diagnostics == ()
    (profile,) = batch.profiles
    assert profile.name == "reader"
    assert profile.tools == expected_tools
    assert profile.denied_tools == expected_denials
    assert profile.mode == mode
    if profile.source_format in {"copilot", "opencode", "antigravity"}:
        assert not profile.user_invocable
    if profile.source_format == "copilot":
        assert profile.allowed_agents == ("reviewer",)
    if profile.source_format == "gemini":
        assert profile.allowed_agents == ()


def test_codex_toml_multiline_prompt_readonly_and_inherited_model(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".codex/agents/audit.toml",
        'name = "audit"\ndescription = "Inspect changes"\nsandbox_mode = "read-only"\ndeveloper_instructions = """\nInspect the diff.\nExplain risks.\n"""\n',
    )
    batch = discover_agents(tmp_path)
    assert batch.diagnostics == ()
    (profile,) = batch.profiles
    assert (profile.name, profile.mode, profile.model) == ("audit", "reviewer", "")
    assert profile.instructions == "Inspect the diff.\nExplain risks."
    assert profile.tools is None
    assert profile.source_format == "codex"


@pytest.mark.parametrize(
    "extra, message",
    [
        ('\nmodel = "gpt-6.1-sol"', "agent_imports model"),
        ('\nsandbox_mode = "workspace-write"', "sandbox_mode"),
        ('\nmodel_reasoning_effort = "high"', "model_reasoning_effort"),
        ('\n[mcp_servers.evil]\ncommand = "touch NEVER"', "mcp_servers"),
        ('\nname = "duplicate"', "overwrite"),
    ],
)
def test_codex_rejects_unmapped_models_and_unsupported_session_settings(
    tmp_path: Path, extra: str, message: str
) -> None:
    _write(
        tmp_path,
        ".codex/agents/audit.toml",
        'name = "audit"\ndescription = "Inspect changes"\ndeveloper_instructions = "Review"' + extra,
    )
    batch = discover_agents(tmp_path)
    assert not batch.profiles
    assert message in batch.diagnostics[0]


@pytest.mark.parametrize(
    "directory, header, message",
    [
        (".claude", "name: audit\ndescription: Review\nhooks: {}", "hooks"),
        (".claude", "name: audit\ndescription: Review\npermissionMode: bypassPermissions", "permissionMode"),
        (".claude", "name: audit\ndescription: Review\ndisallowedTools: MissingTool", "unsupported claude tool"),
        (".github", "description: Review\ntools: [web]", "tool_map"),
        (".github", "description: Review\nhandoffs: []", "handoffs"),
        (".github", "description: Review\nmodel: [one, two]", "model must be"),
        (".github", "description: Review\ntools: [read]\nagents: [audit]", "requires the agent tool"),
        (".github", "description: Review\nmcp-servers: {}", "mcp-servers"),
        (".opencode", "description: Review\npermission:\n  bash:\n    '*': deny", "pattern policies"),
        (".opencode", "description: Review\npermission:\n  read: ask", "ask requires per-call approval"),
        (".opencode", "description: Review\npermission:\n  '*': deny", "unsupported opencode tool"),
        (".opencode", "description: Review\ntools:\n  unknown: false", "unsupported opencode tool"),
        (".cursor", "is_background: true", "is_background"),
        (".gemini", "name: audit\ndescription: Review\nkind: remote", "kind must be local"),
        (".gemini", "name: audit\ndescription: Review\ntools: ['mcp_*']", "tool_map"),
        (".gemini", "name: audit\ndescription: Review\nmcpServers: {}", "mcpServers"),
        (".agents", "name: audit\ndescription: Review\ncommandExecutionPolicy: eager", "commandExecutionPolicy"),
    ],
)
def test_foreign_constraints_fail_closed(tmp_path: Path, directory: str, header: str, message: str) -> None:
    path = _write(tmp_path, f"{directory}/agents/audit.md", _markdown(header))
    batch = discover_agents(tmp_path)
    assert not batch.profiles
    assert str(path) in batch.diagnostics[0]
    assert message in batch.diagnostics[0]


@pytest.mark.parametrize("tools, expected", [("", None), ("\ntools: ['*']", None), ("\ntools: []", ())])
def test_copilot_tool_defaults_and_availability(tmp_path: Path, tools: str, expected: object) -> None:
    _write(
        tmp_path,
        ".github/agents/audit.agent.md",
        _markdown("description: Review\nuser-invocable: true\ndisable-model-invocation: true\nagents: []" + tools),
    )
    batch = discover_agents(tmp_path)
    assert batch.diagnostics == ()
    assert batch.profiles[0].tools == expected
    assert batch.profiles[0].user_invocable
    assert not batch.profiles[0].subagent_available
    assert batch.profiles[0].allowed_agents == ()


def test_antigravity_nested_layout_defaults_to_no_tools(tmp_path: Path) -> None:
    _write(tmp_path, ".agents/agents/reader/agent.md", _markdown("name: reader\ndescription: Review\nsubagent: false"))
    _write(tmp_path, ".agent/agents/ignored.md", _markdown("name: ignored\ndescription: Singular root must be ignored"))
    _write(
        tmp_path, ".agents/agents/arbitrary/nested/agent.md", _markdown("name: ignored2\ndescription: Do not recurse")
    )
    batch = discover_agents(tmp_path)
    assert batch.diagnostics == ()
    (profile,) = batch.profiles
    assert profile.name == "reader"
    assert profile.tools == ()
    assert not profile.subagent_available
    assert profile.user_invocable


def test_native_nested_package_name_comes_from_directory(tmp_path: Path) -> None:
    _write(tmp_path, ".ngn/agents/reader/agent.md", _markdown("description: Inspect"))
    assert discover_agents(tmp_path).profiles[0].name == "reader"


def test_personal_roots_use_xdg_and_gemini_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, config = tmp_path / "home", tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    _write(config, "ngn/agents/native.md", _markdown("description: User native"))
    _write(config, "opencode/agents/global.md", _markdown("description: User OpenCode"))
    _write(home, ".gemini/config/agents/anti/agent.md", _markdown("name: anti\ndescription: User Antigravity"))
    _write(home, ".copilot/agents/copilot.md", _markdown("description: User Copilot"))
    batch = discover_agents(home, personal=True)
    assert batch.diagnostics == ()
    assert {profile.name for profile in batch.profiles} == {"native", "global", "anti", "copilot"}
    assert discover_agents(home).profiles == ()


def test_explicit_imports_map_models_and_tools_with_config_relative_paths(tmp_path: Path) -> None:
    directory = tmp_path / ".ngn"
    directory.mkdir()
    source = _write(
        tmp_path,
        ".claude/agents/research.md",
        _markdown("name: research\ndescription: Search\nmodel: sonnet\ntools: [WebSearch]\ndisallowedTools: Bash"),
    )
    batch = import_agents(
        [
            {
                "path": "../.claude/agents/research.md",
                "format": "claude",
                "model": "gpt-6-luna",
                "provider": "work-login",
                "tool_map": {"WebSearch": ["mcp_web.search"]},
            }
        ],
        directory,
    )
    assert batch.diagnostics == ()
    (profile,) = batch.profiles
    assert (profile.model, profile.provider) == ("gpt-6-luna", "work-login")
    assert profile.tools == ("mcp_web.search",)
    assert profile.denied_tools == ("shell",)
    absolute = import_agents(
        [
            {
                "path": str(source),
                "format": "claude",
                "model": "gpt-6-sol",
                "tool_map": {"WebSearch": ["mcp_web.search"]},
            }
        ],
        directory,
    )
    assert absolute.diagnostics == ()
    assert absolute.profiles[0].model == "gpt-6-sol"


@pytest.mark.parametrize(
    "value, message",
    [
        ({}, "must be a list"),
        (["file.md"], "must be a mapping"),
        ([{"path": "a.md", "format": "unknown"}], "format must be"),
        ([{"path": "a.md", "format": "native", "tools": []}], "unsupported import field"),
        ([{"path": "a.md", "format": "native", "model": ""}], "model override must be nonempty"),
        ([{"path": "a.md", "format": "native", "tool_map": {"Read": []}}], "nonempty lists"),
        ([{"path": "a.md", "format": "native", "tool_map": {"Read": ["*"]}}], "invalid name"),
        ([{"path": "missing.md", "format": "native"}], "No such file"),
    ],
)
def test_invalid_import_entries_are_actionable(tmp_path: Path, value: object, message: str) -> None:
    batch = import_agents(value, tmp_path)
    assert not batch.profiles
    assert message in batch.diagnostics[0]


def test_import_failure_does_not_hide_an_independent_valid_entry(tmp_path: Path) -> None:
    _write(tmp_path, "valid.md", _markdown("description: Valid agent"))
    batch = import_agents(
        [{"path": "missing.md", "format": "native"}, {"path": "valid.md", "format": "native"}], tmp_path
    )
    assert [profile.name for profile in batch.profiles] == ["valid"]
    assert len(batch.diagnostics) == 1


def test_symlink_file_and_root_are_never_followed(tmp_path: Path) -> None:
    source = _write(tmp_path, "outside/agent.md", _markdown("description: Outside"))
    root = tmp_path / "workspace"
    (root / ".ngn/agents").mkdir(parents=True)
    (root / ".ngn/agents/leak.md").symlink_to(source)
    (root / ".claude").mkdir()
    (root / ".claude/agents").symlink_to(source.parent, target_is_directory=True)
    batch = discover_agents(root)
    assert not batch.profiles
    assert len(batch.diagnostics) == 2
    assert all("symlinks" in item for item in batch.diagnostics)
    imported = import_agents([{"path": str(root / ".ngn/agents/leak.md"), "format": "native"}], root)
    assert not imported.profiles
    assert "symlinks" in imported.diagnostics[0]


def test_symlink_directory_inside_import_path_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "outside/agent.md", _markdown("description: Outside"))
    (tmp_path / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    batch = import_agents([{"path": "linked/agent.md", "format": "native"}], tmp_path)
    assert not batch.profiles
    assert "symlinks" in batch.diagnostics[0]


def test_discovery_file_count_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_definitions, "MAX_AGENT_FILES", 2)
    for name in ("c", "a", "b"):
        _write(tmp_path, f".ngn/agents/{name}.md", _markdown("description: Bounded"))
    batch = discover_agents(tmp_path)
    assert [profile.name for profile in batch.profiles] == ["a", "b"]
    assert "exceeded 2 files" in batch.diagnostics[0]


def test_oversized_binary_and_directory_sources_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path, ".ngn/agents/large.md", "x" * (agent_definitions.MAX_AGENT_BYTES + 1))
    binary = _write(tmp_path, ".ngn/agents/binary.md", "")
    binary.write_bytes(b"\xff\xfe\x80")
    batch = discover_agents(tmp_path)
    assert not batch.profiles
    assert len(batch.diagnostics) == 2
    assert any("256 KiB" in item for item in batch.diagnostics)
    imported = import_agents([{"path": ".ngn/agents", "format": "native"}], tmp_path)
    assert "regular file" in imported.diagnostics[0]


def test_oversized_frontmatter_does_not_hide_valid_neighbors(tmp_path: Path) -> None:
    _write(tmp_path, ".ngn/agents/header.md", _markdown("description: " + "x" * agent_definitions.MAX_HEADER_BYTES))
    _write(tmp_path, ".ngn/agents/valid.md", _markdown("description: Valid"))
    batch = discover_agents(tmp_path)
    assert [profile.name for profile in batch.profiles] == ["valid"]
    assert "32 KiB" in batch.diagnostics[0]


def test_foreign_executable_configuration_is_not_loaded(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    _write(
        tmp_path,
        ".claude/agents/hook.md",
        _markdown(f"name: hook\ndescription: Unsafe\nhooks:\n  command: touch {marker}"),
    )
    _write(tmp_path, ".ngn/agents/ignored.py", f"from pathlib import Path\nPath({str(marker)!r}).touch()")
    batch = discover_agents(tmp_path)
    assert not batch.profiles
    assert "hooks" in batch.diagnostics[0]
    assert not marker.exists()
