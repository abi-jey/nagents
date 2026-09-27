"""The documented YAML/env/CLI schema, independent of provider I/O."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Literal
from typing import cast

import pytest

from nagents.cli import _parser
from nagents.harness import Harness
from nagents.harness.config import API_NAMES
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config


def test_new_defaults_and_generic_agent(tmp_path: Path) -> None:
    config = HarnessConfig(workspace=tmp_path)
    assert config.api == "auto"
    assert config.max_subagent_depth == 2
    assert config.agent == "assistant"
    assert config.profile("assistant").mode == "build"
    assert config.theme_background == "auto"


def test_builtin_assistant_only_and_explicit_custom_profiles(tmp_path: Path) -> None:
    config = HarnessConfig(workspace=tmp_path)
    assert config.profile_names == ("assistant",)
    assert config.profile("assistant") == AgentProfile(mode="build")
    for name in ("agent", "build", "reviewer"):
        with pytest.raises(ValueError, match="Unknown agent profile"):
            config.profile(name)
    with pytest.raises(ValueError, match="Unknown agent profile 'reviewer'"):
        HarnessConfig(workspace=tmp_path, agent="reviewer")
    configured = HarnessConfig(
        workspace=tmp_path,
        agent="reviewer",
        profiles={"reviewer": AgentProfile(mode="reviewer")},
    )
    assert configured.profile_names == ("assistant", "reviewer")
    assert configured.profile("reviewer") == AgentProfile(mode="reviewer")
    assert configured.profile("assistant") == AgentProfile(mode="build")
    # The read-only flag changes only the built-in assistant's effective mode.
    read_only = HarnessConfig(workspace=tmp_path, read_only=True)
    assert read_only.profile("assistant") == AgentProfile(mode="reviewer")
    assert read_only.agent == "assistant"
    with pytest.raises(ValueError, match="cannot be overridden"):
        HarnessConfig(workspace=tmp_path, profiles={"assistant": AgentProfile(mode="reviewer")})


def test_submit_mode_defaults_validation_yaml_and_cli(tmp_path: Path) -> None:
    assert HarnessConfig(workspace=tmp_path).submit_mode == "queue"
    assert HarnessConfig(workspace=tmp_path, submit_mode="interrupt").submit_mode == "interrupt"
    with pytest.raises(ValueError, match="submit_mode"):
        HarnessConfig(workspace=tmp_path, submit_mode=cast("Literal['queue', 'interrupt']", "discard"))
    path = tmp_path / "config.yaml"
    path.write_text("submit_mode: interrupt\n")
    assert load_config(tmp_path, path).submit_mode == "interrupt"
    parsed = _parser().parse_args(["--submit-mode", "interrupt", "run", "hello"])
    assert parsed.submit_mode == "interrupt"


@pytest.mark.parametrize("legacy_agent", ["build", "agent", "reviewer"])
def test_load_config_migrates_legacy_agent_from_yaml_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_agent: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"agent: {legacy_agent}\n")
    config = load_config(tmp_path, path)
    assert config.agent == "assistant"
    assert config.read_only is (legacy_agent == "reviewer")
    assert any("Migrated legacy agent selection" in note for note in config.diagnostics)
    # The same migration applies to the NGN_AGENT environment default.
    monkeypatch.setenv("NGN_AGENT", legacy_agent)
    env_config = load_config(tmp_path)
    assert env_config.agent == "assistant"
    assert env_config.read_only is (legacy_agent == "reviewer")


def test_load_config_keeps_explicit_custom_reviewer_identity(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("agent: reviewer\nprofiles:\n  reviewer:\n    mode: reviewer\n")
    config = load_config(tmp_path, path)
    assert config.agent == "reviewer" and config.read_only is False
    assert config.profile("reviewer").mode == "reviewer"
    assert not any("Migrated legacy agent selection" in note for note in config.diagnostics)


def test_load_config_fresh_default_is_only_assistant(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.agent == "assistant" and config.read_only is False
    assert config.profile_names == ("assistant",)
    for name in ("build", "agent", "reviewer"):
        with pytest.raises(ValueError, match="Unknown agent profile"):
            config.profile(name)


def test_load_config_read_only_boolean_from_yaml_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert HarnessConfig(workspace=tmp_path).read_only is False
    path = tmp_path / "config.yaml"
    path.write_text("read_only: true\n")
    assert load_config(tmp_path, path).read_only is True
    monkeypatch.setenv("NGN_READ_ONLY", "1")
    assert load_config(tmp_path).read_only is True
    with pytest.raises(ValueError, match="read_only"):
        HarnessConfig(workspace=tmp_path, read_only=1)  # type: ignore[arg-type]


def test_shipped_config_example_loads_without_code_or_credentials(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[2] / "examples/harness/config.yaml"
    config = load_config(tmp_path, path)
    assert config.theme == "ocean"
    assert config.max_subagent_depth == 2
    assert config.profile("audit").mode == "reviewer"
    assert config.plugins == ()


@pytest.mark.parametrize("api", API_NAMES)
def test_explicit_api_routes_are_configurable(tmp_path: Path, api: str) -> None:
    assert HarnessConfig(workspace=tmp_path, api=api).api == api


@pytest.mark.parametrize("api", API_NAMES[1:])
def test_generic_api_route_never_uses_chatgpt_auth(tmp_path: Path, api: str) -> None:
    with pytest.raises(ValueError, match="ChatGPT login requires"):
        HarnessConfig(workspace=tmp_path, api=api, auth="chatgpt")


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
@pytest.mark.requires_posix
def test_saved_login_does_not_override_explicit_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: str,
) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, api=api, data_dir=tmp_path / "data"))
        monkeypatch.setattr(harness.openai_auth, "logged_in", lambda: True)

        async def forbidden() -> None:
            pytest.fail("An explicitly selected API must not route through ChatGPT OAuth")

        monkeypatch.setattr(harness, "_use_chatgpt", forbidden)
        try:
            await harness.initialize()
            with pytest.raises(ValueError, match="api override"):
                await harness.login(lambda authorization: forbidden())
            assert harness.config.auth == "auto"
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "document",
    [
        "api: unknown",
        "theme_background: transparent",
        "max_subagent_depth: -1",
        "max_subagent_depth: 9",
        "max_subagent_depth: true",
        "max_subagent_depth: 1.5",
        "profiles:\n  assistant:\n    mode: reviewer",
    ],
)
def test_new_fields_reject_invalid_values(tmp_path: Path, document: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(document)
    with pytest.raises(ValueError):
        load_config(tmp_path, path)


@pytest.mark.parametrize("depth", [False, True, 1.5, -1, 9])
def test_depth_is_strict_for_python_configs(tmp_path: Path, depth: int) -> None:
    with pytest.raises(ValueError, match="max_subagent_depth"):
        HarnessConfig(workspace=tmp_path, max_subagent_depth=depth)


def test_scalar_environment_defaults_and_file_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "API": "responses",
        "MAX_SUBAGENT_DEPTH": "3",
        "THEME_BACKGROUND": "terminal",
    }.items():
        monkeypatch.setenv(f"NGN_{key}", value)
    config = load_config(tmp_path)
    assert config.api == "responses" and config.max_subagent_depth == 3
    assert config.theme_background == "terminal"
    path = tmp_path / "config.yaml"
    path.write_text("api: chat_completions\nmax_subagent_depth: 0\n")
    config = load_config(tmp_path, path)
    assert config.api == "chat_completions" and config.max_subagent_depth == 0


def test_cli_overrides(tmp_path: Path) -> None:
    args = _parser().parse_args(
        [
            "--theme-background",
            "theme",
            "run",
            "--api",
            "responses",
            "--max-subagent-depth",
            "0",
            "hello",
        ]
    )
    assert args.theme_background == "theme"
    assert args.api == "responses" and args.max_subagent_depth == 0
    assert args.prompt == ["hello"]
    config = replace(HarnessConfig(workspace=tmp_path), api=args.api, max_subagent_depth=args.max_subagent_depth)
    assert config.api == "responses" and config.max_subagent_depth == 0


def test_historic_dictation_yaml_and_env_are_ignored_but_unknown_keys_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "model: retained-model\n"
        "dictation_enabled: true\ndictation_model: 42\ndictation_base_url: file:///unsafe\n"
        "dictation_api_key_env: literal-secret\ndictation_language: invalid\ndictation_max_seconds: -1\n"
    )
    monkeypatch.setenv("NGN_DICTATION_ENABLED", "true")
    monkeypatch.setenv("NGN_DICTATION_MAX_SECONDS", "not-an-integer")
    config = load_config(tmp_path, path)
    assert config.model == "retained-model"
    assert not any(key.startswith("dictation_") for key in vars(config))
    path.write_text(path.read_text() + "unknown_preference: true\n")
    with pytest.raises(ValueError, match="unknown_preference"):
        load_config(tmp_path, path)


@pytest.mark.parametrize("flag", ["--dictation", "--no-dictation", "--dictation-model"])
def test_removed_cli_flags_fail(flag: str) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args([flag, "run", "hello"])


def test_same_name_profiles_replace_whole_profile_only_in_trusted_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user"))
    user = tmp_path / "user/ngn"
    user.mkdir(parents=True)
    (user / "config.yaml").write_text(
        "agent: audit\nprofiles:\n  audit:\n    mode: reviewer\n    model: audit-model\n"
        "    instructions: User instructions\n  other:\n    mode: reviewer\n"
    )
    project = tmp_path / ".ngn"
    project.mkdir()
    (project / "config.yaml").write_text("profiles:\n  audit:\n    instructions: Project instructions\n")
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("profiles:\n")

    with pytest.warns(UserWarning, match="Ignoring untrusted project"):
        untrusted = load_config(tmp_path, explicit)
    assert untrusted.profile("audit") == AgentProfile("reviewer", "User instructions", "audit-model")

    trusted = load_config(tmp_path, explicit, trust_project=True)
    # Documented replacement resets omitted mode/model; an empty mapping deletes nothing.
    assert trusted.profile("audit") == AgentProfile("build", "Project instructions", "")
    assert trusted.profile("other") == AgentProfile("reviewer")

    explicit.write_text("profiles:\n  audit:\n    mode: reviewer\n")
    overridden = load_config(tmp_path, explicit, trust_project=True)
    assert overridden.profile("audit") == AgentProfile("reviewer")
    assert overridden.profile("other") == AgentProfile("reviewer")
