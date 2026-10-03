from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.events import ErrorEvent
from nagents.events import Event
from nagents.events import FinishReason
from nagents.events import ReasoningChunkEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.web import live_summary
from nagents.web.live_summary import VoiceContextSummarizer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Sequence

    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition
    from nagents.web.live_summary import SummaryResult


class SummaryProvider(Provider):
    def __init__(self, events: Sequence[Event]) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, api_key="test", model="gpt-6-astra", api="responses")
        self.events = events
        self.requests: list[list[Message]] = []
        self.closed = 0
        self.wait: asyncio.Event | None = None

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        assert tools == []
        assert config is None, "OAuth does not support output-token or sampling controls"
        assert stream and not verify_model
        self.requests.append(messages)
        try:
            if self.wait is not None:
                await self.wait.wait()
            for event in self.events:
                yield event
        finally:
            self.closed += 1


async def prepare(
    summarizer: VoiceContextSummarizer, provider: Provider, fingerprint: str = "snapshot"
) -> SummaryResult:
    return await summarizer.prepare(
        source='{"history": [{"role":"user", "text":"Project is called Juniper."}]}',
        fingerprint=fingerprint,
        session_id="ngn-voice-test",
        provider=provider,
    )


@pytest.mark.asyncio
async def test_summary_is_read_only_and_never_promotes_history_or_reasoning_to_instructions() -> None:
    provider = SummaryProvider(
        [
            ReasoningChunkEvent(chunk="Private reasoning."),
            TextChunkEvent(chunk="Project is Juniper."),
            TextDoneEvent(text="Project is Juniper."),
        ]
    )
    result = await prepare(VoiceContextSummarizer(), provider)
    assert result.text == "Project is Juniper."
    assert result.reason == "generated"
    assert provider.closed == 1
    instructions, history = provider.requests[0]
    assert instructions.role == "system" and "Juniper" not in str(instructions.content)
    assert history.role == "user" and "Juniper" in str(history.content)
    assert "Private reasoning" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "reason"),
    [
        ([TextChunkEvent(chunk="Unconfirmed fragment")], "invalid"),
        ([TextDoneEvent(text="text"), ErrorEvent(message="secret provider response")], "unavailable"),
        ([ToolCallEvent(name="delete_files", arguments={})], "invalid"),
        ([TextDoneEvent(text="incomplete", finish_reason=FinishReason.LENGTH)], "invalid"),
        ([TextDoneEvent(text=" ")], "invalid"),
        ([TextDoneEvent(text="first"), TextDoneEvent(text="second")], "invalid"),
        ([TextDoneEvent(text="Friday"), TextChunkEvent(chunk="Actually Monday")], "invalid"),
        ([TextDoneEvent(text="Friday"), ReasoningChunkEvent(chunk="Still thinking")], "invalid"),
        ([TextChunkEvent(chunk="字" * 1001)], "too_long"),
        ([TextDoneEvent(text="字" * 1001)], "too_long"),
    ],
)
async def test_summary_rejects_incomplete_unsafe_or_oversize_results(events: list[Event], reason: str) -> None:
    provider = SummaryProvider(events)
    result = await prepare(VoiceContextSummarizer(), provider)
    assert result.text == "" and result.reason == reason
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_summary_cache_is_scoped_to_root_source_provider_model_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [10.0]
    monkeypatch.setattr("nagents.web.live_summary.time.monotonic", lambda: clock[0])
    summaries = VoiceContextSummarizer()
    provider = SummaryProvider([TextDoneEvent(text="Juniper")])
    await prepare(summaries, provider)
    assert (await prepare(summaries, provider)).cached
    assert len(provider.requests) == 1
    await prepare(summaries, provider, "changed")
    provider.model = "another-model"
    await prepare(summaries, provider)
    assert len(provider.requests) == 3
    other = SummaryProvider([TextDoneEvent(text="Another provider")])
    assert not (await prepare(summaries, other)).cached
    await summaries.prepare(
        source="different root", fingerprint="snapshot", session_id="ngn-another", provider=provider
    )
    assert len(provider.requests) == 4
    clock[0] += live_summary.SUMMARY_CACHE_SECONDS + 1
    assert not (await prepare(summaries, provider)).cached
    summaries.clear()
    assert not (await prepare(summaries, provider)).cached


@pytest.mark.asyncio
async def test_summary_cache_evicts_old_entries_and_failures_are_not_cached() -> None:
    summaries = VoiceContextSummarizer()
    provider = SummaryProvider([TextDoneEvent(text="Juniper")])
    for index in range(live_summary.SUMMARY_CACHE_ENTRIES + 1):
        await prepare(summaries, provider, str(index))
    assert not (await prepare(summaries, provider, "0")).cached
    provider.events = [ErrorEvent(message="Credentials unavailable")]
    assert not (await prepare(summaries, provider, "failed")).text
    provider.events = [TextDoneEvent(text="Now available")]
    assert (await prepare(summaries, provider, "failed")).text == "Now available"


@pytest.mark.asyncio
async def test_summary_timeout_closes_generator_and_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SummaryProvider([])
    provider.wait = asyncio.Event()
    monkeypatch.setattr(live_summary, "SUMMARY_SECONDS", 0.01)
    result = await prepare(VoiceContextSummarizer(), provider)
    assert result.reason == "timeout" and provider.closed == 1
    monkeypatch.setattr(live_summary, "SUMMARY_SECONDS", 60.0)
    task = asyncio.create_task(prepare(VoiceContextSummarizer(), provider))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.closed == 2


@pytest.mark.asyncio
async def test_summary_rejects_bad_or_oversize_sources_without_calling_provider() -> None:
    provider = SummaryProvider([])
    summaries = VoiceContextSummarizer()
    for source, reason in [(" ", "empty"), ("字" * 32001, "too_long"), ("\ud800", "invalid")]:
        result = await summaries.prepare(
            source=source, fingerprint="snapshot", session_id="ngn-test", provider=provider
        )
        assert result.reason == reason
    assert provider.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("coalesced", [False, True])
async def test_summary_cleanup_has_a_separate_grace_deadline(monkeypatch: pytest.MonkeyPatch, coalesced: bool) -> None:
    closed = asyncio.Event()

    class SlowCleanupProvider(SummaryProvider):
        async def generate(
            self,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            try:
                await asyncio.sleep(10)
                yield TextDoneEvent(text="Unused")
            finally:
                try:
                    await asyncio.sleep(10)
                finally:
                    closed.set()

    monkeypatch.setattr(live_summary, "SUMMARY_SECONDS", 0.01)
    monkeypatch.setattr(live_summary, "SUMMARY_CLEANUP_SECONDS", 0.02)
    if coalesced:
        timeout = asyncio.timeout

        def due_in_same_turn(delay: float) -> asyncio.Timeout:
            # Reproduce a coarse clock or delayed loop where all prearmed
            # deadlines fire before the cancelled task gets its next turn.
            return timeout(0)

        monkeypatch.setattr(asyncio, "timeout", due_in_same_turn)
    task = asyncio.create_task(prepare(VoiceContextSummarizer(), SlowCleanupProvider([])))
    try:
        done, _ = await asyncio.wait({task}, timeout=1.0)
        assert task in done, "Summary cleanup exceeded its separate grace deadline"
        assert task.result().reason == "timeout"
        assert closed.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_cancel", [False, True])
@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_caller_cancellation_joins_the_provider_after_bounded_cleanup(
    monkeypatch: pytest.MonkeyPatch, repeat_cancel: bool, cleanup_error: bool
) -> None:
    started, cleaning, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    workers: list[asyncio.Task[object]] = []

    class BlockingCleanupProvider(SummaryProvider):
        async def generate(
            self,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            worker = asyncio.current_task()
            assert worker is not None
            workers.append(worker)
            try:
                started.set()
                await asyncio.Event().wait()
                yield TextDoneEvent(text="Unused")
            finally:
                cleaning.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()
                    if cleanup_error:
                        raise RuntimeError("Private provider cleanup failure")

    monkeypatch.setattr(live_summary, "SUMMARY_SECONDS", 60)
    monkeypatch.setattr(live_summary, "SUMMARY_CLEANUP_SECONDS", 0.02)
    task = asyncio.create_task(prepare(VoiceContextSummarizer(), BlockingCleanupProvider([])))
    try:
        await started.wait()
        task.cancel()
        await cleaning.wait()
        if repeat_cancel:
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "Caller cancellation abandoned provider cleanup"
        with pytest.raises(asyncio.CancelledError):
            task.result()
        assert closed.is_set() and all(worker.done() for worker in workers)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
