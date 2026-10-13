"""Portable effort settings use native fields without changing provider defaults."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents.adapters.responses import format_request
from nagents.designer.runtime import DesignProvider
from nagents.designer.schema import STARTER
from nagents.designer.schema import parse
from nagents.designer.store import Recorder
from nagents.events import ErrorEvent
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.provider import OpenAIProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider.context import ContextMetadata
from nagents.provider.openai import CodexCredentials
from nagents.provider.openai import _request_body
from nagents.provider.reasoning import catalog_reasoning_efforts
from nagents.provider.reasoning import reasoning_effort_options
from nagents.provider.reasoning import validate_reasoning_effort
from nagents.types import GenerationConfig
from nagents.types import ToolDefinition
from tests.providers.test_gateway_provider import payload
from tests.providers.test_provider_reliability import candidate
from tests.support.config import connection

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("kind", "model", "expected"),
    [
        ("openai", "gpt-6.1-sol", ("low", "medium", "high", "xhigh", "max")),
        ("openai", "gpt-6-astra", ("low", "medium", "high", "xhigh", "max")),
        ("openai", "gpt-6-luna", ("none", "low", "medium", "high", "xhigh", "max")),
        ("openai", "gpt-5.1", ("none", "low", "medium", "high")),
        ("openai", "gpt-5.2-2025-12-11", ("none", "low", "medium", "high", "xhigh")),
        ("openai", "gpt-5", ("minimal", "low", "medium", "high")),
        ("openai", "gpt-5.5-chat-latest", ()),
        ("openai", "gpt-4o", ()),
        ("openai", "gpt-future", ()),
        ("anthropic", "claude-opus-4-5", ("low", "medium", "high")),
        ("anthropic", "claude-opus-4-6", ("low", "medium", "high", "max")),
        ("anthropic", "claude-opus-4-7", ("low", "medium", "high", "xhigh", "max")),
        ("anthropic", "claude-sonnet-4-5", ()),
        ("anthropic", "claude-opus-5-5", ("low", "medium", "high", "xhigh", "max")),
        ("gemini", "gemini-3.8-flash", ("low", "medium", "high")),
        ("google", "gemini-3.8-flash", ("low", "medium", "high")),
        ("gemini", "gemini-3-flash-preview", ("minimal", "low", "medium", "high")),
        ("gemini", "models/gemini-3.1-pro-preview", ("low", "medium", "high")),
        ("gemini", "gemini-2.5-flash", ()),
        ("openrouter", "openai/gpt-6.1-sol", ("low", "medium", "high", "xhigh", "max")),
    ],
)
def test_documented_choices_are_model_specific(kind: str, model: str, expected: tuple[str, ...]) -> None:
    assert reasoning_effort_options(kind, model) == expected


@pytest.mark.parametrize("value", [None, True, 3, "default", "adaptive", "ultra", [], {}])
def test_effort_enum_rejects_invalid_input(value: object) -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        validate_reasoning_effort(value)


def test_custom_routes_and_invalid_contracts_have_no_guessed_choices() -> None:
    assert not reasoning_effort_options("openai_compatible", "gpt-6.1-sol", base_url="https://gateway.example/v1")
    assert not reasoning_effort_options("openai", "gpt-6.1-sol", api="messages")
    assert not reasoning_effort_options("anthropic", "claude-opus-5", base_url="https://gateway.example")
    assert not reasoning_effort_options("litellm", "openai/gpt-6.1-sol")
    assert not reasoning_effort_options("openai", "gpt-6.1-sol", auth="codex", api="responses")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "model", "api", "expected"),
    [
        (ProviderType.OPENAI_COMPATIBLE, "gpt-6.1-sol", "chat_completions", {"reasoning_effort": "low"}),
        (ProviderType.OPENAI_COMPATIBLE, "gpt-6.1-sol", "responses", {"reasoning": {"effort": "low"}}),
        (ProviderType.ANTHROPIC, "claude-opus-4-5", "messages", {"output_config": {"effort": "low"}}),
        (
            ProviderType.GEMINI_NATIVE,
            "gemini-3.8-flash",
            "auto",
            {"generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}}},
        ),
        (ProviderType.OPENROUTER, "openai/gpt-6.1-sol", "chat_completions", {"reasoning": {"effort": "low"}}),
    ],
)
async def test_portable_effort_maps_to_native_wire_fields(
    kind: ProviderType, model: str, api: str, expected: dict[str, object]
) -> None:
    async with Provider(kind, "offline", model, api=api) as provider:
        response = candidate([{"text": "ok"}]) if api == "auto" else payload(api)
        with patch.object(provider._http, "post_json", AsyncMock(return_value=response)) as request:
            events = [
                event
                async for event in provider.generate([], config=GenerationConfig(reasoning_effort="low"), stream=False)
            ]
        assert not any(isinstance(event, ErrorEvent) for event in events)
        body = request.call_args.args[1]
        for key, value in expected.items():
            assert body[key] == value
        if kind == ProviderType.ANTHROPIC:
            assert "thinking" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-4o", "custom-model"])
async def test_unsupported_effort_is_rejected_before_http(model: str) -> None:
    async with Provider(ProviderType.OPENAI_COMPATIBLE, "offline", model) as provider:
        with patch.object(provider._http, "post_json", AsyncMock()) as request:
            events = [
                event
                async for event in provider.generate([], config=GenerationConfig(reasoning_effort="none"), stream=False)
            ]
        request.assert_not_called()
        assert len(events) == 1 and isinstance(events[0], ErrorEvent)
        assert "not supported" in events[0].message


def test_codex_and_responses_preserve_defaults_and_override_only_effort() -> None:
    authored = {"effort": "medium", "summary": "concise"}
    config = GenerationConfig(responses_reasoning={"effort": "medium", "summary": "concise"}, reasoning_effort="max")
    assert _request_body("gpt-6.1-sol", [], None, config)["reasoning"] == {"effort": "max", "summary": "concise"}
    assert format_request("gpt-6.1-sol", [], None, config, False)["reasoning"] == {
        "effort": "max",
        "summary": "concise",
    }
    assert config.responses_reasoning == authored
    assert "reasoning" not in _request_body("gpt-6.1-sol", [], None, GenerationConfig())
    assert "reasoning" not in format_request("gpt-6.1-sol", [], None, GenerationConfig(), False)


@pytest.mark.asyncio
async def test_gemini_effort_replaces_budget_without_mutating_authored_config() -> None:
    config = GenerationConfig(
        reasoning_effort="low", thinking_config={"thinkingBudget": 1024, "includeThoughts": False}
    )
    async with Provider(ProviderType.GEMINI_NATIVE, "offline", "gemini-3.8-flash") as provider:
        with patch.object(provider._http, "post_json", AsyncMock(return_value=candidate([{"text": "ok"}]))) as request:
            _ = [event async for event in provider.generate([], config=config, stream=False)]
        assert request.call_args.args[1]["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "low",
            "includeThoughts": False,
        }
        assert config.thinking_config == {"thinkingBudget": 1024, "includeThoughts": False}


@pytest.mark.asyncio
async def test_effort_and_summary_are_independent_on_responses() -> None:
    async with Provider(ProviderType.OPENAI_COMPATIBLE, "offline", "gpt-6.1-sol", api="responses") as provider:
        with patch.object(provider._http, "post_json", AsyncMock(return_value=payload("responses"))) as request:
            _ = [
                event
                async for event in provider.generate(
                    [], config=GenerationConfig(reasoning_effort="low", reasoning_summary="auto"), stream=False
                )
            ]
        assert request.call_args.args[1]["reasoning"] == {"effort": "low", "summary": "auto"}


@pytest.mark.asyncio
async def test_openrouter_effort_overrides_conflicting_controls_without_mutation() -> None:
    config = GenerationConfig(reasoning_effort="low", reasoning={"max_tokens": 1024, "enabled": False})
    async with Provider(ProviderType.OPENROUTER, "offline", "openai/gpt-6.1-sol") as provider:
        with patch.object(provider._http, "post_json", AsyncMock(return_value=payload("chat_completions"))) as request:
            _ = [event async for event in provider.generate([], config=config, stream=False)]
        assert request.call_args.args[1]["reasoning"] == {"effort": "low"}
        assert config.reasoning == {"max_tokens": 1024, "enabled": False}


def test_catalog_capabilities_are_exact_bounded_and_clear_on_account_change() -> None:
    metadata = ContextMetadata()
    metadata.select(("provider", "endpoint", "account-one"))
    metadata.update(
        [
            {"slug": "custom-codex", "supported_reasoning_levels": [{"effort": "high"}, {"effort": "low"}]},
            {"slug": "explicit-no-effort", "supported_reasoning_levels": []},
            {"slug": "malformed", "supported_reasoning_levels": [{"effort": "future-unknown"}]},
        ],
        id_key="slug",
    )
    assert metadata.reasoning_efforts == {"custom-codex": ("low", "high"), "explicit-no-effort": ()}
    copied = metadata.copy()
    copied.select(("provider", "endpoint", "account-two"))
    assert not copied.reasoning_efforts
    assert metadata.reasoning_efforts["custom-codex"] == ("low", "high")
    metadata.update([{"slug": "custom-codex"}], id_key="slug")
    assert "custom-codex" not in metadata.reasoning_efforts


def test_openrouter_catalog_mandatory_and_null_efforts() -> None:
    available, choices = catalog_reasoning_efforts({"reasoning": {"supported_efforts": None, "mandatory": True}})
    assert available and "none" not in choices and "max" in choices
    assert catalog_reasoning_efforts({"reasoning": {"supported_efforts": ["low", "high"]}}) == (True, ("low", "high"))
    assert catalog_reasoning_efforts({"reasoning": {"supported_efforts": ["unrecognized"]}}) == (False, ())


@pytest.mark.asyncio
async def test_codex_catalog_options_apply_to_draft_model_without_mutation() -> None:
    async def credentials() -> CodexCredentials:
        return CodexCredentials("offline-token", "offline-account")

    provider = OpenAIProvider(credentials, model="gpt-6.1-sol")
    async with provider:
        provider._remember_context_auth("offline-token", "offline-account", "")
        provider._context_metadata.select(provider._context_metadata_identity())
        provider._context_metadata.update(
            [
                {"slug": "draft-only-model", "supported_reasoning_levels": [{"effort": "high"}]},
                {"slug": "gpt-6.1-sol", "supported_reasoning_levels": [{"effort": "medium"}]},
            ],
            id_key="slug",
        )
        assert provider.cached_reasoning_efforts("draft-only-model") == ("high",)
        assert provider.cached_reasoning_efforts() == ("medium",)
        assert provider.model == "gpt-6.1-sol"
        provider._remember_context_auth("other-token", "other-account", "")
        assert provider.cached_reasoning_efforts("draft-only-model") == ()
        assert provider.cached_reasoning_efforts() == ("low", "medium", "high", "xhigh", "max")


@pytest.mark.asyncio
async def test_harness_auto_routes_gpt6_tools_to_responses_and_preserves_explicit_routes(tmp_path: Path) -> None:
    config = HarnessConfig(workspace=tmp_path, providers=connection(), model="gpt-4o")
    async with HarnessProvider(config) as provider:
        assert provider.api == "chat_completions"
        for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol-2026-10-01"):
            provider.model = model
            assert provider.api == "responses"
        provider.model = "gpt-4o"
        assert provider.api == "chat_completions"
    explicit = HarnessConfig(workspace=tmp_path, providers=connection(api="chat_completions"), model="gpt-6.1-sol")
    async with HarnessProvider(explicit) as provider:
        assert provider.api == "chat_completions"
    custom = HarnessConfig(
        workspace=tmp_path,
        providers=connection("openai_compatible", base_url="https://gateway.example/v1", auth="api-key"),
        model="gpt-6.1-sol",
    )
    async with HarnessProvider(custom) as provider:
        assert provider.api == "chat_completions"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"])
async def test_designer_and_framework_share_native_gpt6_auto_routing(model: str) -> None:
    design = parse(STARTER)
    design.providers["primary"].model = model
    async with DesignProvider(design, "primary", Recorder(), demo=False) as provider:
        assert provider.api == "responses"
        # Exercise actual Provider dispatch while isolating credentials and HTTP.
        tools = [ToolDefinition("read_file", "Read workspace text", {"type": "object", "properties": {}})]
        with patch.object(provider._http, "post_json", AsyncMock(return_value=payload("responses"))) as request:
            events = [
                event
                async for event in Provider.generate(
                    provider, [], tools=tools, config=GenerationConfig(reasoning_effort="high"), stream=False
                )
            ]
        assert not any(isinstance(event, ErrorEvent) for event in events)
        assert request.call_args.args[0].endswith("/responses")
        assert request.call_args.args[1]["reasoning"] == {"effort": "high"}
        assert request.call_args.args[1]["tools"][0]["name"] == "read_file"
    design.providers["primary"].api = "chat_completions"
    async with DesignProvider(design, "primary", Recorder(), demo=False) as provider:
        assert provider.api == "chat_completions"
    async with Provider(
        ProviderType.OPENAI_COMPATIBLE, "offline", model, base_url="https://gateway.example/v1"
    ) as provider:
        assert provider.api == "chat_completions"
