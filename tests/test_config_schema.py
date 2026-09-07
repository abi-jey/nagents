"""The documented TOML/env/CLI schema, independent of microphone or provider I/O."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from nagents.cli import _parser
from nagents.harness import Harness
from nagents.harness.config import API_NAMES
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config


def test_new_defaults_and_generic_agent(tmp_path: Path) -> None:
    config = HarnessConfig(workspace=tmp_path)
    assert config.api == "auto"
    assert config.max_subagent_depth == 2
    assert config.profile("agent").mode == "build"
    assert config.theme_background == "auto"
    assert not config.dictation_enabled
    assert config.dictation_model == "gpt-4o-mini-transcribe"
    assert config.dictation_base_url == "https://api.openai.com/v1"
    assert config.dictation_api_key_env == "OPENAI_API_KEY"
    assert config.dictation_language == ""
    assert config.dictation_max_seconds == 120


def test_shipped_config_example_loads_without_code_or_credentials(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[1] / "examples/harness/config.toml"
    config = load_config(tmp_path, path)
    assert config.theme == "ocean"
    assert config.max_subagent_depth == 2
    assert config.profile("audit").mode == "reviewer"
    assert config.plugins == ()
    assert not config.dictation_enabled


@pytest.mark.parametrize("api", API_NAMES)
def test_explicit_api_routes_are_configurable(tmp_path: Path, api: str) -> None:
    assert HarnessConfig(workspace=tmp_path, api=api).api == api


@pytest.mark.parametrize("api", API_NAMES[1:])
def test_generic_api_route_never_uses_chatgpt_auth(tmp_path: Path, api: str) -> None:
    with pytest.raises(ValueError, match="ChatGPT login requires"):
        HarnessConfig(workspace=tmp_path, api=api, auth="chatgpt")


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
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
    "toml",
    [
        'api = "unknown"',
        'theme_background = "transparent"',
        "max_subagent_depth = -1",
        "max_subagent_depth = 9",
        "max_subagent_depth = true",
        "max_subagent_depth = 1.5",
        'dictation_enabled = "true"',
        'dictation_model = "  "',
        'dictation_api_key_env = "not-an-env-name"',
        'dictation_base_url = "file:///tmp/audio"',
        'dictation_base_url = "https://user:password@example.invalid/v1"',
        'dictation_base_url = "https://example.invalid/v1?key=placeholder"',
        'dictation_base_url = "https://example.invalid/v1#fragment"',
        'dictation_base_url = "https://exa mple.invalid/v1"',
        'dictation_language = "english"',
        'dictation_language = "EN"',
        "dictation_max_seconds = 0",
        "dictation_max_seconds = 301",
        "dictation_max_seconds = false",
        "dictation_max_seconds = 1.5",
        '[profiles.agent]\nmode = "reviewer"',
    ],
)
def test_new_fields_reject_invalid_values(tmp_path: Path, toml: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(toml)
    with pytest.raises(ValueError):
        load_config(tmp_path, path)


@pytest.mark.parametrize("depth", [False, True, 1.5, -1, 9])
def test_depth_is_strict_for_python_configs(tmp_path: Path, depth: int) -> None:
    with pytest.raises(ValueError, match="max_subagent_depth"):
        HarnessConfig(workspace=tmp_path, max_subagent_depth=depth)


def test_scalar_environment_defaults_and_toml_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "API": "responses",
        "MAX_SUBAGENT_DEPTH": "3",
        "THEME_BACKGROUND": "terminal",
        "DICTATION_ENABLED": "1",
        "DICTATION_MODEL": "gateway-transcription",
        "DICTATION_BASE_URL": "http://127.0.0.1:4000/v1",
        "DICTATION_API_KEY_ENV": "NGN_TEST_TRANSCRIPTION_KEY",
        "DICTATION_LANGUAGE": "de",
        "DICTATION_MAX_SECONDS": "45",
    }.items():
        monkeypatch.setenv(f"NGN_{key}", value)
    config = load_config(tmp_path)
    assert config.api == "responses" and config.max_subagent_depth == 3
    assert config.theme_background == "terminal" and config.dictation_enabled
    assert config.dictation_model == "gateway-transcription"
    assert config.dictation_base_url == "http://127.0.0.1:4000/v1"
    assert config.dictation_api_key_env == "NGN_TEST_TRANSCRIPTION_KEY"
    assert config.dictation_language == "de" and config.dictation_max_seconds == 45
    path = tmp_path / "config.toml"
    path.write_text('api = "chat_completions"\nmax_subagent_depth = 0\ndictation_enabled = false\n')
    config = load_config(tmp_path, path)
    assert config.api == "chat_completions" and config.max_subagent_depth == 0
    assert not config.dictation_enabled
    assert config.dictation_language == "de"


def test_cli_overrides_and_disable_dictation(tmp_path: Path) -> None:
    args = _parser().parse_args(
        [
            "--dictation",
            "--theme-background",
            "theme",
            "run",
            "--no-dictation",
            "--api",
            "responses",
            "--max-subagent-depth",
            "0",
            "--dictation-model",
            "test-transcriber",
            "--dictation-language",
            "en",
            "--dictation-max-seconds",
            "30",
            "hello",
        ]
    )
    assert not args.dictation_enabled
    assert args.theme_background == "theme"
    assert args.api == "responses" and args.max_subagent_depth == 0
    assert args.dictation_model == "test-transcriber" and args.dictation_language == "en"
    assert args.dictation_max_seconds == 30
    assert args.prompt == ["hello"]
    config = replace(HarnessConfig(workspace=tmp_path), api=args.api, max_subagent_depth=args.max_subagent_depth)
    assert config.api == "responses" and config.max_subagent_depth == 0
