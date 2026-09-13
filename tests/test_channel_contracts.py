"""Public channel discovery and listener ownership contracts, without services."""

import asyncio
from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from nagents import Agent
from nagents import Channel
from nagents import ChannelDelivery
from nagents import ChannelMessage
from nagents import ChannelReceiver
from nagents import ChannelSend
from nagents import ChannelValue
from nagents import Message
from nagents import SessionManager
from nagents.channels import plugins
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.extensions import AgentPlugin
from nagents.extensions import RunContext
from nagents.types import GenerationConfig
from nagents.types import ToolDefinition
from tests.test_extensions import OfflineProvider


class IdleChannel(Channel):
    def __init__(self, name: str = "idle") -> None:
        self.name = name
        self.started = asyncio.Event()
        self.closed = False

    async def listen(self, receive: ChannelReceiver) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        raise AssertionError("No model-authorized send was expected")

    async def close(self) -> None:
        self.closed = True


class FiniteChannel(IdleChannel):
    async def listen(self, receive: ChannelReceiver) -> None:
        self.started.set()
        await receive(ChannelMessage("one", "conversation", "sender", text="go"))


class ClosingProvider(OfflineProvider):
    def __init__(self) -> None:
        super().__init__()
        self.closing = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closing.set()
        await self.release_close.wait()
        await super().close()


def factory(config: dict[str, ChannelValue]) -> Channel:
    return IdleChannel(str(config["name"]))


def test_explicit_plugin_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def installed(*, group: str, name: str) -> tuple[EntryPoint, ...]:
        calls.append((group, name))
        return (EntryPoint(name, "tests.test_channel_contracts:factory", group),)

    monkeypatch.setattr(plugins, "entry_points", installed)
    connector = plugins.load_channel("example", {"name": "updates"})
    assert connector.name == "updates"
    assert calls == [("nagents.channels", "example")]
    assert isinstance(connector, IdleChannel) and not connector.started.is_set()


@pytest.mark.parametrize("count", [0, 2])
def test_plugin_discovery_rejects_missing_or_ambiguous(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    def installed(*, group: str, name: str) -> tuple[EntryPoint, ...]:
        return (EntryPoint(name, "tests.test_channel_contracts:factory", group),) * count

    monkeypatch.setattr(plugins, "entry_points", installed)
    with pytest.raises(ValueError, match="Expected one"):
        plugins.load_channel("example", {})


def test_fluent_attachment_and_listener_exclusive_ownership(tmp_path: Path) -> None:
    async def scenario() -> None:
        agent = Agent(OfflineProvider(), SessionManager(tmp_path / "identity.db"), compactor=None)
        channel = IdleChannel()
        assert agent.add_channel(channel) is agent
        with pytest.raises(ValueError, match="already attached"):
            agent.add_channel(IdleChannel())
        listener = asyncio.create_task(agent.listen("identity"))
        await asyncio.wait_for(channel.started.wait(), 5)
        try:
            with pytest.raises(RuntimeError, match="already executing"):
                await agent.listen("other")
            with pytest.raises(RuntimeError, match="while listening"):
                agent.add_channel(IdleChannel("second"))
            with pytest.raises(RuntimeError, match="listener owns"):
                async for _ in agent.run("outside", session_id="identity"):
                    pass
            with pytest.raises(RuntimeError, match="listener owns"):
                async for _ in agent.run_simple([Message(role="user", content="outside")]):
                    pass
            with pytest.raises(RuntimeError, match="listener owns"):
                await agent.compact("identity")
            with pytest.raises(RuntimeError, match="listener owns"):
                await agent.clear_session("identity")
        finally:
            await asyncio.wait_for(agent.close(), 5)
        assert listener.cancelled() and channel.closed
        assert not agent._channel_listening and agent._active_runs == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("simple", [False, True])
def test_listener_rejects_inflight_generation_and_early_close_releases_ownership(tmp_path: Path, simple: bool) -> None:
    async def scenario() -> None:
        class GatedProvider(OfflineProvider):
            async def generate(
                self,
                messages: list[Message],
                tools: list[ToolDefinition] | None = None,
                config: GenerationConfig | None = None,
                stream: bool = True,
                verify_model: bool = False,
            ) -> AsyncIterator[Event]:
                yield TextDoneEvent(text="first")
                await asyncio.Event().wait()

        agent = Agent(GatedProvider(), SessionManager(tmp_path / "active.db"), compactor=None)
        agent.add_channel(IdleChannel())
        events = agent.run_simple([Message(role="user", content="go")]) if simple else agent.run("go")
        await anext(events)
        try:
            with pytest.raises(RuntimeError, match="already executing"):
                await agent.listen("identity")
        finally:
            await events.aclose()
            await agent.close()
        assert agent._active_runs == 0

    asyncio.run(scenario())


def test_close_rejects_new_work_through_provider_cleanup_and_allows_reuse(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ClosingProvider()
        channel = IdleChannel()
        agent = Agent(provider, SessionManager(tmp_path / "reuse.db"), compactor=None).add_channel(channel)
        await agent.initialize()
        listener = asyncio.create_task(agent.listen("identity"))
        await asyncio.wait_for(channel.started.wait(), 5)
        closing = asyncio.create_task(agent.close())
        try:
            await asyncio.wait_for(provider.closing.wait(), 5)
            assert listener.cancelled() and channel.closed
            assert not agent._channel_listening
            with pytest.raises(RuntimeError, match="closing"):
                await asyncio.wait_for(agent.listen("replacement"), 1)
            with pytest.raises(RuntimeError, match="closing"):
                async for _ in agent.run("outside", session_id="identity"):
                    pass
            with pytest.raises(RuntimeError, match="closing"):
                async for _ in agent.run_simple([Message(role="user", content="outside")]):
                    pass
            with pytest.raises(RuntimeError, match="closing"):
                await agent.compact("identity")
            with pytest.raises(RuntimeError, match="closing"):
                await agent.clear_session("identity")
            with pytest.raises(RuntimeError, match="closing"):
                await agent.initialize()
            with pytest.raises(RuntimeError, match="closing"):
                agent.add_channel(IdleChannel("second"))
        finally:
            provider.release_close.set()
            await asyncio.wait_for(closing, 5)
        assert agent._active_runs == 0 and not agent.is_initialized
        await agent.close()
        assert provider.close_calls == 1

        # Lazy initialization reopens a successfully closed Agent.
        events = [event async for event in agent.run("again", session_id="identity")]
        assert any(isinstance(event, TextDoneEvent) and event.text == "answer" for event in events)
        assert provider.verifications == 2 and agent.is_initialized
        await agent.close()
        await agent.close()
        assert provider.close_calls == 2

        # Even an idle listener reopens its resource lifetime, without a model run.
        channel.started.clear()
        listener = asyncio.create_task(agent.listen("identity"))
        await asyncio.wait_for(channel.started.wait(), 5)
        await asyncio.wait_for(agent.close(), 5)
        assert listener.cancelled() and provider.close_calls == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("original_fails", [False, True])
def test_replaced_provider_gets_its_own_close_without_reinitialization(tmp_path: Path, original_fails: bool) -> None:
    async def scenario() -> None:
        class OriginalProvider(ClosingProvider):
            async def close(self) -> None:
                await super().close()
                if original_fails:
                    raise RuntimeError("original cleanup failed")

        original = OriginalProvider()
        original.release_close.set()
        replacement = ClosingProvider()
        replacement.release_close.set()
        agent = Agent(original, SessionManager(tmp_path / "provider-replacement.db"), compactor=None)
        if original_fails:
            with pytest.raises(RuntimeError, match="original cleanup failed"):
                await agent.close()
        else:
            await agent.close()

        # Match Harness auth switching: replace after close, without model I/O
        # or Agent.initialize() before the next application shutdown.
        agent.provider = replacement
        await asyncio.wait_for(asyncio.gather(agent.close(), agent.close()), 5)
        await agent.close()
        assert original.close_calls == replacement.close_calls == 1
        assert original.verifications == replacement.verifications == 0
        assert not agent._closing and not agent.is_initialized

    asyncio.run(scenario())


def test_concurrent_close_callers_join_cleanup_despite_repeated_cancellation(tmp_path: Path) -> None:
    async def scenario() -> None:
        class ClosingChannel(IdleChannel):
            def __init__(self) -> None:
                super().__init__()
                self.closing = asyncio.Event()
                self.release_close = asyncio.Event()
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1
                self.closing.set()
                await self.release_close.wait()
                await super().close()

        provider = ClosingProvider()
        channel = ClosingChannel()
        agent = Agent(provider, SessionManager(tmp_path / "cancel-close.db"), compactor=None).add_channel(channel)
        listener = asyncio.create_task(agent.listen("identity"))
        await asyncio.wait_for(channel.started.wait(), 5)
        first = asyncio.create_task(agent.close())
        second = asyncio.create_task(agent.close())
        third = asyncio.create_task(agent.close())
        try:
            await asyncio.wait_for(channel.closing.wait(), 5)
            first.cancel()
            second.cancel()
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            assert not first.done() and not second.done() and not third.done()
            assert channel.close_calls == 1 and provider.close_calls == 0
            channel.release_close.set()
            await asyncio.wait_for(provider.closing.wait(), 5)
            first.cancel()
            second.cancel()
            await asyncio.sleep(0)
            assert not first.done() and not second.done() and not third.done()
            assert channel.closed and provider.close_calls == 1
        finally:
            channel.release_close.set()
            provider.release_close.set()
            results = await asyncio.wait_for(asyncio.gather(first, second, third, return_exceptions=True), 5)
        assert isinstance(results[0], asyncio.CancelledError)
        assert isinstance(results[1], asyncio.CancelledError)
        assert results[2] is None and listener.cancelled()
        await agent.close()
        assert provider.close_calls == 1 and channel.close_calls == 1
        assert not agent._closing

    asyncio.run(scenario())


def test_close_preserves_failures_and_attempts_all_resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        class FailingProvider(ClosingProvider):
            async def close(self) -> None:
                await super().close()
                raise RuntimeError("provider cleanup failed")

        batch_calls = 0

        async def close_batch() -> None:
            nonlocal batch_calls
            batch_calls += 1
            raise ValueError("batch cleanup failed")

        provider = FailingProvider()
        agent = Agent(provider, SessionManager(tmp_path / "close-errors.db"), batch=True, compactor=None)
        assert agent._batch_client is not None
        monkeypatch.setattr(agent._batch_client, "close", close_batch)
        first = asyncio.create_task(agent.close())
        second = asyncio.create_task(agent.close())
        await asyncio.wait_for(provider.closing.wait(), 5)
        provider.release_close.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5)
        error = results[0]
        assert isinstance(error, ExceptionGroup) and results[1] is error
        assert [str(item) for item in error.exceptions] == ["provider cleanup failed", "batch cleanup failed"]
        with pytest.raises(ExceptionGroup) as repeated:
            await agent.close()
        assert repeated.value is error
        assert provider.close_calls == batch_calls == 1
        assert not agent._closing and not agent.is_initialized
        await agent.initialize()
        with pytest.raises(ExceptionGroup):
            await agent.close()
        assert provider.close_calls == batch_calls == 2

    asyncio.run(scenario())


def test_nested_after_run_cleanup_retains_channel_ownership(tmp_path: Path) -> None:
    async def scenario() -> None:
        cleaned: list[str] = []

        class Cleanup(AgentPlugin):
            async def after_run(self, context: RunContext) -> None:
                await context.agent.clear_session(context.session_id)
                if context.session_id == "identity":
                    async for _ in context.agent.run("nested", session_id="nested"):
                        pass
                    # The inner cleanup must not revoke its parent's ownership.
                    await context.agent.clear_session(context.session_id)
                cleaned.append(context.session_id)

        provider = OfflineProvider([[TextDoneEvent(text="outer")], [TextDoneEvent(text="inner")]])
        agent = Agent(
            provider, SessionManager(tmp_path / "nested.db"), compactor=None, plugins=[Cleanup()]
        ).add_channel(FiniteChannel())
        try:
            await asyncio.wait_for(agent.listen("identity"), 5)
            assert cleaned == ["nested", "identity"]
            assert await agent.session.get_history("identity") == []
            assert await agent.session.get_history("nested") == []
            assert not agent._cleanup_tasks and agent._active_runs == 0
        finally:
            await agent.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("external_close", [False, True])
def test_after_run_rejects_self_close_but_can_clear_during_shutdown(tmp_path: Path, external_close: bool) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        cleaned = asyncio.Event()

        class Cleanup(AgentPlugin):
            async def after_run(self, context: RunContext) -> None:
                started.set()
                await release.wait()
                with pytest.raises(RuntimeError, match="another task"):
                    await context.agent.close()
                await context.agent.clear_session(context.session_id)
                if external_close:
                    with pytest.raises(RuntimeError, match="closing"):
                        async for _ in context.agent.run("new", session_id="new"):
                            pass
                    with pytest.raises(RuntimeError, match="closing"):
                        async for _ in context.agent.run_simple([Message(role="user", content="new")]):
                            pass
                    with pytest.raises(RuntimeError, match="closing"):
                        await context.agent.compact(context.session_id)
                cleaned.set()

        agent = Agent(
            OfflineProvider(), SessionManager(tmp_path / "hook-close.db"), compactor=None, plugins=[Cleanup()]
        ).add_channel(FiniteChannel())
        listener = asyncio.create_task(agent.listen("identity"))
        await asyncio.wait_for(started.wait(), 5)
        if external_close:
            closing = asyncio.create_task(agent.close())
            await asyncio.sleep(0)
            assert agent._closing
            release.set()
            await asyncio.wait_for(closing, 5)
            assert listener.cancelled()
        else:
            release.set()
            await asyncio.wait_for(listener, 5)
            await agent.close()
        assert cleaned.is_set() and await agent.session.get_history("identity") == []
        assert not agent._cleanup_tasks and agent._active_runs == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("simple", [False, True])
def test_early_close_waits_for_retained_provider_generator_before_handoff(tmp_path: Path, simple: bool) -> None:
    async def scenario() -> None:
        order: list[str] = []

        class StreamProvider(OfflineProvider):
            def __init__(self) -> None:
                super().__init__()
                self.cleaning = asyncio.Event()
                self.release_cleanup = asyncio.Event()
                self.source = self.events()

            async def events(self) -> AsyncGenerator[Event, None]:
                try:
                    yield TextDoneEvent(text="first")
                    await asyncio.Event().wait()
                finally:
                    self.cleaning.set()
                    await self.release_cleanup.wait()
                    order.append("provider")

            def generate(
                self,
                messages: list[Message],
                tools: list[ToolDefinition] | None = None,
                config: GenerationConfig | None = None,
                stream: bool = True,
                verify_model: bool = False,
            ) -> AsyncIterator[Event]:
                return self.source

        class Cleanup(AgentPlugin):
            async def after_run(self, context: RunContext) -> None:
                assert order == ["provider"]
                order.append("hook")

        provider = StreamProvider()
        channel = IdleChannel()
        agent = Agent(
            provider,
            SessionManager(tmp_path / "stream-close.db"),
            compactor=None,
            plugins=[] if simple else [Cleanup()],
        ).add_channel(channel)
        events = agent.run_simple([Message(role="user", content="go")]) if simple else agent.run("go")
        await anext(events)
        closing = asyncio.create_task(events.aclose())
        try:
            await asyncio.wait_for(provider.cleaning.wait(), 5)
            with pytest.raises(RuntimeError, match="already executing"):
                await agent.listen("identity")
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done() and agent._active_runs == 1
        finally:
            provider.release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 5)
        assert order == (["provider"] if simple else ["provider", "hook"])
        with pytest.raises(StopAsyncIteration):
            await anext(provider.source)
        assert not agent._cleanup_tasks and agent._active_runs == 0
        listener = asyncio.create_task(agent.listen("identity"))
        await asyncio.wait_for(channel.started.wait(), 5)
        await asyncio.wait_for(agent.close(), 5)
        assert listener.cancelled()

    asyncio.run(scenario())


@pytest.mark.parametrize("simple", [False, True])
def test_provider_iterator_without_aclose_remains_supported(tmp_path: Path, simple: bool) -> None:
    async def scenario() -> None:
        class Events(AsyncIterator[Event]):
            def __init__(self) -> None:
                self.pending = True

            async def __anext__(self) -> Event:
                if not self.pending:
                    raise StopAsyncIteration
                self.pending = False
                return TextDoneEvent(text="plain iterator")

        class IteratorProvider(OfflineProvider):
            def generate(
                self,
                messages: list[Message],
                tools: list[ToolDefinition] | None = None,
                config: GenerationConfig | None = None,
                stream: bool = True,
                verify_model: bool = False,
            ) -> AsyncIterator[Event]:
                return Events()

        agent = Agent(IteratorProvider(), SessionManager(tmp_path / "iterator.db"), compactor=None)
        try:
            stream = agent.run_simple([Message(role="user", content="go")]) if simple else agent.run("go")
            events = [event async for event in stream]
            assert isinstance(events[0], TextDoneEvent) and events[0].text == "plain iterator"
            assert agent._active_runs == 0
        finally:
            await agent.close()

    asyncio.run(scenario())
