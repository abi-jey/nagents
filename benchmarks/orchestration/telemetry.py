"""Per-generation usage accounting, including owned children and compaction calls."""

from __future__ import annotations

import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import Usage
from nagents.harness.provider import HarnessProvider
from nagents.provider.openai import OpenAIProvider
from nagents.types import RetryConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable

    from nagents.events import Event
    from nagents.harness.config import HarnessConfig
    from nagents.harness.credentials import ProviderLoginStore
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition


@dataclass
class Generation:
    provider: int
    role: str
    tools_present: bool
    started: float = field(default_factory=time.monotonic)
    seconds: float = 0.0
    done: bool = False
    usage_reported: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    def observe(self, event: Event) -> None:
        # Provider usage is cumulative for ONE generation and may be repeated
        # on tool and text events. Replace it, never add per-event counts.
        usage = event.usage
        if usage.has_usage():
            self.usage_reported = True
            for name in ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens", "total_tokens"):
                setattr(self, name, getattr(usage, name))
        if isinstance(event, TextDoneEvent):
            self.done = True
        if isinstance(event, ErrorEvent):
            self.errors.append(event.code or "provider_error")


@dataclass
class Meter:
    max_requests: int = 32
    max_observed_tokens: int = 150_000
    providers: int = 0
    calls: list[Generation] = field(default_factory=list)

    def admit(self, provider: int, tools_present: bool) -> Generation:
        # No await between checking the run-wide budget and reserving a request.
        if len(self.calls) >= self.max_requests:
            raise RuntimeError("benchmark_request_budget")
        if sum(call.total_tokens for call in self.calls) >= self.max_observed_tokens:
            raise RuntimeError("benchmark_observed_token_budget")
        call = Generation(provider, "root" if provider == 0 else "child", tools_present)
        self.calls.append(call)
        return call

    def summary(self) -> dict[str, object]:
        result: dict[str, object] = {"provider_generations": len(self.calls), "providers_created": self.providers}
        for name in ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens", "total_tokens"):
            result[name] = sum(getattr(call, name) for call in self.calls)
        result["uncached_prompt_tokens"] = sum(max(0, call.prompt_tokens - call.cached_tokens) for call in self.calls)
        result["usage_complete"] = all(call.done and call.usage_reported for call in self.calls) and bool(self.calls)
        result["generations_without_complete_usage"] = sum(
            not (call.done and call.usage_reported) for call in self.calls
        )
        result["root_generations"] = sum(call.role == "root" for call in self.calls)
        result["child_generations"] = sum(call.role == "child" for call in self.calls)
        result["no_tools_generations"] = sum(not call.tools_present for call in self.calls)
        result["calls"] = [asdict(call) for call in self.calls]
        return result


class MeasuredProvider(HarnessProvider):
    """Keep native Harness cloning while binding its transport to local OpenAI auth."""

    def __init__(self, config: HarnessConfig, meter: Meter) -> None:
        super().__init__(config, request_timeout=90)
        self.live = OpenAIProvider(model=config.model, timeout=90, retry_config=RetryConfig(max_retries=0))
        self.meter = meter
        self.provider_index = meter.providers
        meter.providers += 1

    async def verify_model(self, force: bool = False) -> bool:
        return await self.live.verify_model(force)

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        try:
            call = self.meter.admit(self.provider_index, bool(tools))
        except RuntimeError as error:
            yield ErrorEvent(message=str(error), code="BENCHMARK_BUDGET", recoverable=False)
            return
        self.live.model = self.model
        try:
            async for event in self.live.generate(messages, tools, config, stream, verify_model):
                call.observe(event)
                yield event
        finally:
            call.seconds = time.monotonic() - call.started

    async def close(self) -> None:
        await self.live.close()
        await super().close()


def provider_factory(meter: Meter) -> Callable[[HarnessConfig, ProviderLoginStore | None], MeasuredProvider]:
    def build(config: HarnessConfig, login_store: ProviderLoginStore | None = None) -> MeasuredProvider:
        return MeasuredProvider(config, meter)

    return build


def example_usage(prompt: int, completion: int, cached: int = 0, reasoning: int = 0) -> Usage:
    """Small typed fixture helper for offline telemetry checks."""
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
    )
