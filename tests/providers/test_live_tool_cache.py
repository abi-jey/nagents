"""Opt-in real current-provider cache test; synthetic content, no credential logging.

Run NGN_LIVE_TOOL_CACHE=1 python -m pytest -s tests/providers/test_live_tool_cache.py
Wire logs are private temporary files. A zero-hit warm baseline is skipped, never
reported as proof of cache preservation. This test uses the configured Codex model.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from nagents.events import ErrorEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import load_config
from nagents.observation import observer
from nagents.provider.codex.openai import OpenAIProvider
from nagents.types import Message
from nagents.types import ToolDefinition

_LIVE_ENV = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME")}


@pytest.fixture
def isolated_user_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore real configuration only for this explicitly opted-in live module."""
    if os.environ.get("NGN_LIVE_TOOL_CACHE") != "1":
        return
    for name, value in _LIVE_ENV.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@pytest.mark.skipif(os.environ.get("NGN_LIVE_TOOL_CACHE") != "1", reason="explicit live API opt-in required")
@pytest.mark.asyncio
async def test_current_provider_additions_preserve_measured_cache() -> None:
    config = load_config(Path.cwd())
    assert config.provider_profile().auth == "codex", "This probe targets the currently configured Codex route"
    directory = Path(tempfile.mkdtemp(prefix="ngn-library-tool-cache-"))
    wire_path = directory / "wire.jsonl"
    phase = ""
    requests: list[dict[str, object]] = []
    usages: dict[str, tuple[int, int]] = {}

    def observe(kind: str, data: dict[str, object]) -> None:
        with wire_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"phase": phase, "kind": kind, **data}, default=str) + "\n")
        if kind == "http_request_body":
            requests.append(json.loads(str(data["body"])))

    run = uuid4().hex
    reference = "\n".join(
        f"Synthetic record {index:04d}: immutable reference value {index * 7919 % 104729:06d}." for index in range(3500)
    )
    messages = [
        Message("system", f"Synthetic library cache test {run}. Reply only OK; never call tools."),
        Message("user", reference + "\nReply only OK."),
    ]
    tools = [ToolDefinition("baseline_probe", "Return baseline.", {"type": "object", "properties": {}})]
    token = observer.set(observe)
    try:
        async with OpenAIProvider(model=config.model, timeout=90) as provider:
            provider.set_session_id(run)
            for phase in ("cold", "warm", "repeat", "addition", "continued", "removal"):
                if phase == "addition":
                    tools.append(ToolDefinition("added_probe", "Return added value.", {"type": "object"}))
                if phase == "continued":
                    messages.extend([Message("assistant", "OK"), Message("user", "Next turn. Reply only OK.")])
                if phase == "removal":
                    tools.pop()
                async for event in provider.generate(messages, tools=tools):
                    assert not isinstance(event, ErrorEvent), getattr(event, "message", "")
                    if event.usage.reported:
                        usages[phase] = (event.usage.prompt_tokens, event.usage.cached_tokens)
        # Validate the actual library wire output before interpreting cache counters.
        base, added, continued, removed = requests[0], requests[3], requests[4], requests[5]
        assert added.get("tools") == base.get("tools")
        base_input, added_input = base["input"], added["input"]
        assert isinstance(base_input, list) and isinstance(added_input, list)
        assert added_input[: len(base_input)] == base_input
        assert any(item.get("type") == "additional_tools" for item in added_input if isinstance(item, dict))
        continued_input, removed_input = continued["input"], removed["input"]
        assert isinstance(continued_input, list) and isinstance(removed_input, list)
        assert continued_input[: len(added_input)] == added_input
        assert removed.get("tools") == base.get("tools")
        assert not any(item.get("type") == "additional_tools" for item in removed_input)
        print("Live usage (input, cached):", usages)
        print("Synthetic wire log:", wire_path)
        (directory / "summary.json").write_text(
            json.dumps({"model": config.model, "usage": usages, "wire_prefix_verified": True}, indent=2) + "\n",
            encoding="utf-8",
        )
        if max(usages["warm"][1], usages["repeat"][1]) == 0:
            pytest.skip("Current API reports no baseline cache hits; wire prefix verified, cache benefit unproven")
        assert usages["addition"][1] >= max(usages["warm"][1], usages["repeat"][1]) * 0.8
        assert usages["continued"][1] >= usages["addition"][1] * 0.8
    finally:
        observer.reset(token)


@pytest.mark.skipif(os.environ.get("NGN_LIVE_TOOL_CACHE") != "1", reason="explicit live API opt-in required")
@pytest.mark.asyncio
async def test_current_provider_can_call_newly_appended_tool() -> None:
    config = load_config(Path.cwd())
    assert config.provider_profile().auth == "codex"
    messages = [Message("system", "Synthetic tool integration test."), Message("user", "Reply only OK.")]
    tool = ToolDefinition("newly_loaded_probe", "Return a synthetic test value.", {"type": "object", "properties": {}})
    async with OpenAIProvider(model=config.model, timeout=90) as provider:
        async for event in provider.generate(messages, tools=[]):
            assert not isinstance(event, ErrorEvent), getattr(event, "message", "")
        messages.extend(
            [
                Message("assistant", "OK"),
                Message("user", "Call newly_loaded_probe exactly once with no arguments. Do not answer in text."),
            ]
        )
        calls = []
        async for event in provider.generate(messages, tools=[tool]):
            assert not isinstance(event, ErrorEvent), getattr(event, "message", "")
            if isinstance(event, ToolCallEvent):
                calls.append(event)
        assert len(calls) == 1
        assert calls[0].name == tool.name
        assert calls[0].arguments == {}
