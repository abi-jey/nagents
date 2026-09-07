"""Offline extension and durable tool lifecycle tests, driven by asyncio.run."""

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Lock

import aiosqlite
import pytest

from nagents import Agent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.compactor import Messages
from nagents.events import CompactionDoneEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.exceptions import ToolHallucinationError
from nagents.extensions import AgentPlugin
from nagents.extensions import CompactionRequest
from nagents.extensions import CompactionResult
from nagents.extensions import ModelRequest
from nagents.extensions import RunContext
from nagents.tools import ToolExecutor
from nagents.tools import ToolRegistry
from nagents.types import AudioContent
from nagents.types import DocumentContent
from nagents.types import GenerationConfig
from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import TextContent
from nagents.types import ToolCall
from nagents.types import ToolDefinition


class OfflineProvider(Provider):
    def __init__(self, rounds: list[list[Event]] | None = None) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "offline-key", "offline-model")
        self.rounds = rounds if rounds is not None else [[TextDoneEvent(text="answer")]]
        self.requests: list[ModelRequest] = []
        self.verifications = 0

    async def verify_model(self, force: bool = False) -> bool:
        self.verifications += 1
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        self.requests.append(
            ModelRequest(
                deepcopy(messages),
                [replace(tool, parameters=deepcopy(tool.parameters)) for tool in tools or []],
                deepcopy(config),
            )
        )
        for event in self.rounds[len(self.requests) - 1]:
            yield deepcopy(event)


class ReplacementStrategy:
    def __init__(self, messages: list[Message], automatic: bool = False) -> None:
        self.messages = messages
        self.automatic = automatic
        self.checks: list[CompactionRequest] = []
        self.compactions: list[CompactionRequest] = []

    async def should_compact(self, request: CompactionRequest) -> bool:
        self.checks.append(request)
        return self.automatic and len(request.messages) >= 3

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        self.compactions.append(request)
        return CompactionResult(deepcopy(self.messages), "Python replacement")


async def collect(agent: Agent, message: str = "go", session_id: str = "s") -> list[Event]:
    return [event async for event in agent.run(message, session_id=session_id)]


def test_legacy_import_flow_and_default_hooks(tmp_path: Path) -> None:
    async def drive() -> None:
        provider = OfflineProvider()
        agent = Agent(provider, SessionManager(tmp_path / "legacy.db"), compactor=None)
        assert agent.plugins == []
        assert agent.save_tool_outputs is True
        context = RunContext(agent, "s", "u")
        assert context.round_number == 0
        plugin = AgentPlugin()
        message = Message(role="user", content="go")
        request = ModelRequest([message], [], None)
        call = ToolCall("id", "tool")
        result = ToolResultEvent(id="id", name="tool")
        assert await plugin.before_run(context, message) is message
        assert await plugin.before_model(context, request) is request
        assert await plugin.before_tool(context, call) is call
        assert await plugin.after_tool(context, result) is result
        await plugin.on_event(context, result)
        await plugin.after_run(context)
        assert CompactionResult([message]).summary == ""
        events = await collect(agent)
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].final_text == "answer"
        assert await agent.session.get_history("s") == [message, Message(role="assistant", content="answer")]
        await agent.close()

    asyncio.run(drive())


def test_custom_compaction_reopen_preserves_raw_history_and_serialization(tmp_path: Path) -> None:
    async def drive() -> None:
        db_path = tmp_path / "compact.db"
        session = SessionManager(db_path)
        await session.get_or_create_session("s", "u")
        original = [Message(role="system", content="persisted system"), Message(role="user", content="long history")]
        for message in original:
            await session.add_message("s", message)
        replacements = [
            Message(role="developer", content="retained facts"),
            Message(
                role="assistant",
                content="tool explanation",
                tool_calls=[ToolCall("a", "lookup", {"key": "value"}, {"signature": "sig"})],
            ),
            Message(role="tool", content="lookup result", tool_call_id="a", name="lookup"),
            Message(
                role="user",
                content=[
                    TextContent("next"),
                    ImageContent("aW1hZ2U=", "image/png"),
                    AudioContent("YXVkaW8=", "wav"),
                    DocumentContent("ZG9j", title="notes"),
                ],
            ),
        ]
        provider = OfflineProvider()
        strategy = ReplacementStrategy(replacements)
        agent = Agent(
            provider, session, system_prompt="configured system", compactor=None, compaction_strategy=strategy
        )
        result = await agent.compact("s")
        assert result.summary_text == "Python replacement"
        assert result.new_message_count == len(replacements)
        assert strategy.checks == []  # Manual compaction is forced.
        request = strategy.compactions[0]
        assert request.messages == original
        assert request.provider is provider
        assert request.force is True and request.estimated_tokens > 0
        assert provider.requests == [] and provider.verifications == 0
        assert await session.get_message_count("s") == len(original) + len(replacements)

        reopened = SessionManager(db_path)
        assert await reopened.get_history("s") == replacements
        assert await reopened.get_history("s", limit=2) == replacements[-2:]
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT content FROM v2_messages WHERE session_id = ? ORDER BY id LIMIT 2", ("s",)
            )
            assert [row[0] for row in await cursor.fetchall()] == [message.content for message in original]
            cursor = await db.execute("SELECT compacted_at_message_id FROM v2_sessions WHERE id = ?", ("s",))
            assert await cursor.fetchone() == (3,)
        # Repeated replacements append again and keep the first new row active.
        agent.session = reopened
        await agent.compact("s")
        assert strategy.compactions[-1].messages == replacements
        assert await reopened.get_history("s") == replacements
        assert await reopened.get_message_count("s") == len(original) + 2 * len(replacements)
        await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("manual", [False, True])
def test_legacy_self_compaction_still_works(tmp_path: Path, manual: bool) -> None:
    async def drive() -> None:
        provider = OfflineProvider([[TextDoneEvent(text="legacy summary")], [TextDoneEvent(text="answer")]])
        session = SessionManager(tmp_path / "legacy_compaction.db")
        agent = Agent(provider, session, compactor="self", compact_on=Messages(length=1))
        if manual:
            await session.get_or_create_session("s", "u")
            await session.add_message("s", Message(role="user", content="old context"))
            event = await agent.compact("s")
        else:
            events = await collect(agent)
            event = next(event for event in events if isinstance(event, CompactionDoneEvent))
        assert event.summary_text == "legacy summary"
        assert (await session.get_history("s"))[0] == Message(role="compaction_summary", content="legacy summary")
        assert await session.get_message_count("s") == (2 if manual else 3)
        assert len(provider.requests) == (1 if manual else 2)
        await agent.close()

    asyncio.run(drive())


def test_declined_strategy_mutation_is_not_persisted_or_sent(tmp_path: Path) -> None:
    async def drive() -> None:
        class Decline:
            async def should_compact(self, request: CompactionRequest) -> bool:
                request.messages[0].content = "mutated"
                return False

            async def compact(self, request: CompactionRequest) -> CompactionResult:
                raise AssertionError("Declined compaction must not run")

        provider = OfflineProvider()
        agent = Agent(provider, SessionManager(tmp_path / "decline.db"), compaction_strategy=Decline())
        await collect(agent)
        assert provider.requests[0].messages == [Message(role="user", content="go")]
        assert (await agent.session.get_history("s"))[0].content == "go"
        await agent.close()

    asyncio.run(drive())


def test_stateful_tool_callable_is_not_deep_copied(tmp_path: Path) -> None:
    async def drive() -> None:
        class Stateful:
            def __init__(self) -> None:
                self.lock = Lock()
                self.executed = False

            def work(self) -> str:
                """Use state that cannot be deep copied."""
                with self.lock:
                    self.executed = True
                return "local result"

        owner = Stateful()
        provider = OfflineProvider([[ToolCallEvent(id="a", name="work")], [TextDoneEvent(text="done")]])
        agent = Agent(
            provider,
            SessionManager(tmp_path / "stateful.db"),
            tools=[owner.work],
            plugins=[AgentPlugin()],
            compactor=None,
        )
        await collect(agent)
        assert owner.executed is True
        assert provider.requests[0].tools[0].func == owner.work
        await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("forced", [False, True])
def test_strategy_precedence_each_generation_and_trigger(tmp_path: Path, forced: bool) -> None:
    async def drive() -> None:
        provider = OfflineProvider([[ToolCallEvent(id="a", name="work")], [TextDoneEvent(text="done")]])
        replacement = [Message(role="developer", content="compact facts"), Message(role="user", content="continue")]
        strategy = ReplacementStrategy(replacement, automatic=not forced)
        agent = Agent(
            provider,
            SessionManager(tmp_path / "auto.db"),
            system_prompt="configured",
            compactor="self",
            compact_on=Messages(length=1),  # Would force an extra LLM call if legacy won.
            compaction_strategy=strategy,
        )

        async def work() -> str:
            """Do local work."""
            if forced:
                agent.trigger_compaction()
            return "large result"

        agent.register_tool(work)
        events = await collect(agent)
        assert len(provider.requests) == 2
        assert [len(request.messages) for request in strategy.checks] == ([1] if forced else [1, 3])
        assert len(strategy.compactions) == 1
        assert strategy.compactions[0].force is forced
        assert all(message.content != "configured" for message in strategy.compactions[0].messages)
        assert provider.requests[-1].messages == [Message(role="system", content="configured"), *replacement]
        assert len([event for event in events if isinstance(event, CompactionDoneEvent)]) == 1
        assert await SessionManager(agent.session.db_path).get_history("s") == [
            *replacement,
            Message(role="assistant", content="done"),
        ]
        assert await agent.session.get_message_count("s") == 6
        await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize(
    "replacement",
    [
        [],
        [Message(role="tool", tool_call_id="missing", content="orphan")],
        [Message(role="assistant", tool_calls=[ToolCall("a", "work")])],
        [Message(role="user", tool_calls=[ToolCall("a", "work")])],
        [
            Message(role="assistant", tool_calls=[ToolCall("a", "work")]),
            Message(role="tool", tool_call_id="a", name="wrong"),
        ],
        [
            Message(role="assistant", tool_calls=[ToolCall("a", "work"), ToolCall("a", "work")]),
            Message(role="tool", tool_call_id="a"),
        ],
        [
            Message(role="assistant", tool_calls=[ToolCall("a", "work")]),
            Message(role="user", content="interleaved"),
            Message(role="tool", tool_call_id="a"),
        ],
        [
            Message(role="assistant", tool_calls=[ToolCall("a", "work")]),
            Message(role="tool", tool_call_id="a"),
            Message(role="tool", tool_call_id="a"),
        ],
    ],
)
def test_invalid_strategy_rejected_before_database_write(tmp_path: Path, replacement: list[Message]) -> None:
    async def drive() -> None:
        session = SessionManager(tmp_path / "invalid.db")
        await session.get_or_create_session("s", "u")
        original = [Message(role="user", content="keep me")]
        await session.replace_context("s", original)
        before = await session.get_session("s")
        agent = Agent(OfflineProvider(), session, compaction_strategy=ReplacementStrategy(replacement))
        with pytest.raises(ValueError):
            await agent.compact("s")
        assert await session.get_history("s") == original
        assert await session.get_message_count("s") == 1
        assert await session.get_session("s") == before
        await agent.close()

    asyncio.run(drive())


def test_replace_context_rolls_back_all_inserts_on_failure(tmp_path: Path) -> None:
    async def drive() -> None:
        session = SessionManager(tmp_path / "transaction.db")
        await session.get_or_create_session("s", "u")
        original = [Message(role="user", content="keep")]
        await session.replace_context("s", original)
        before = await session.get_session("s")
        async with aiosqlite.connect(session.db_path) as db:
            await db.execute(
                """CREATE TRIGGER reject_developer BEFORE INSERT ON v2_messages
                   WHEN NEW.role = 'developer' BEGIN SELECT RAISE(ABORT, 'test failure'); END"""
            )
            await db.commit()
        with pytest.raises(aiosqlite.IntegrityError, match="test failure"):
            await session.replace_context("s", [Message(role="user", content="first"), Message(role="developer")])
        assert await session.get_history("s") == original
        assert await session.get_message_count("s") == 1
        assert await session.get_session("s") == before
        with pytest.raises(ValueError, match="not found"):
            await session.replace_context("absent", original)
        assert await session.get_message_count("absent") == 0

    asyncio.run(drive())


def test_before_model_detaches_messages_tools_and_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def drive() -> None:
        session = SessionManager(tmp_path / "ephemeral.db")
        await session.get_or_create_session("s", "u")
        original = Message(role="user", content=[TextContent("original")])
        await session.add_message("s", original)
        returned_histories: list[list[Message]] = []
        get_history = session.get_history

        async def tracked_history(session_id: str, limit: int | None = None) -> list[Message]:
            history = await get_history(session_id, limit)
            returned_histories.append(history)
            return history

        monkeypatch.setattr(session, "get_history", tracked_history)

        class Transform(AgentPlugin):
            async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
                assert isinstance(request.messages[0].content, list)
                part = request.messages[0].content[0]
                assert isinstance(part, TextContent)
                part.text = "ephemeral"
                request.messages.pop()
                request.tools[0].parameters["properties"]["value"]["description"] = "ephemeral schema"
                assert request.config is not None and request.config.stop is not None
                request.config.stop.append("ephemeral stop")
                request.config.temperature = 0.2
                return request

        def work(value: str) -> str:
            """Return a value."""
            return value

        provider = OfflineProvider()
        agent = Agent(provider, session, tools=[work], plugins=[Transform()], compactor=None)
        definition = deepcopy(agent.tool_registry.get_all()[0])
        config = GenerationConfig(temperature=0.7, stop=["original stop"])
        _ = [event async for event in agent.run("new", "s", config=config)]
        assert provider.requests[0].messages == [Message(role="user", content=[TextContent("ephemeral")])]
        assert provider.requests[0].config == GenerationConfig(
            temperature=0.2, stop=["original stop", "ephemeral stop"]
        )
        assert config == GenerationConfig(temperature=0.7, stop=["original stop"])
        assert agent.tool_registry.get_all()[0] == definition
        assert all(history[0] == original for history in returned_histories)
        assert (await session.get_history("s"))[:2] == [original, Message(role="user", content="new")]
        await agent.close()

    asyncio.run(drive())


def test_ordered_hooks_transform_before_executor_and_persist(tmp_path: Path) -> None:
    async def drive() -> None:
        trace: list[str] = []
        provider = OfflineProvider(
            [
                [TextDoneEvent(text="calling"), ToolCallEvent(id="a", name="work", arguments={"value": "x"})],
                [TextDoneEvent(text="done")],
            ]
        )

        class Plugin(AgentPlugin):
            def __init__(self, label: str) -> None:
                self.label = label

            async def before_run(self, context: RunContext, message: Message) -> Message:
                assert context.agent is agent and context.session_id == "s" and context.user_id == "u"
                trace.append(f"{self.label}:run")
                message.content = f"{message.content}{self.label}"
                return message

            async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
                trace.append(f"{self.label}:model:{context.round_number}")
                return request

            async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
                trace.append(f"{self.label}:tool")
                call.arguments["value"] = f"{call.arguments['value']}{self.label}"
                return call

            async def after_tool(self, context: RunContext, result: ToolResultEvent) -> ToolResultEvent:
                trace.append(f"{self.label}:result")
                result.result = f"{result.result}{self.label}"
                return result

            async def on_event(self, context: RunContext, event: Event) -> None:
                trace.append(f"{self.label}:event:{event.type.value}")
                if isinstance(event, ToolResultEvent):
                    history = await context.agent.session.get_history(context.session_id)
                    assert history[-1].content == "xABAB"
                    event.id = "observer mutation does not change delivered event"

            async def after_run(self, context: RunContext) -> None:
                trace.append(f"{self.label}:cleanup")

        async def work(value: str) -> str:
            """Return a value."""
            trace.append("execute")
            history = await agent.session.get_history("s")
            assert history[-1].content == "calling"
            assert history[-1].tool_calls == [ToolCall("a", "work", {"value": "xAB"})]
            return value

        agent = Agent(
            provider,
            SessionManager(tmp_path / "hooks.db"),
            tools=[work],
            plugins=[Plugin("A"), Plugin("B")],
            compactor=None,
        )
        message = Message(role="user", content="go")
        events = [event async for event in agent.run(message, "s", "u")]
        assert message.content == "go"
        assert trace == [
            "A:run",
            "B:run",
            "A:model:0",
            "B:model:0",
            "A:event:text_done",
            "B:event:text_done",
            "A:tool",
            "B:tool",
            "A:event:tool_call",
            "B:event:tool_call",
            "execute",
            "A:result",
            "B:result",
            "A:event:tool_result",
            "B:event:tool_result",
            "A:model:1",
            "B:model:1",
            "A:event:text_done",
            "B:event:text_done",
            "A:event:done",
            "B:event:done",
            "A:cleanup",
            "B:cleanup",
        ]
        tool_result = next(event for event in events if isinstance(event, ToolResultEvent))
        assert tool_result.id == "a" and tool_result.result == "xABAB"
        assert (await agent.session.get_history("s"))[0].content == "goAB"
        await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("constructor_injection", [False, True])
def test_injected_executor_denies_transformed_tool(tmp_path: Path, constructor_injection: bool) -> None:
    async def drive() -> None:
        executed: list[str] = []
        audited: list[ToolCall] = []

        def work(value: str) -> str:
            """A protected operation."""
            executed.append(value)
            return value

        class Transform(AgentPlugin):
            async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
                call.arguments["value"] = "restricted"
                return call

        class DenyingExecutor(ToolExecutor):
            async def execute(self, tool_call: ToolCall) -> ToolResultEvent:
                audited.append(deepcopy(tool_call))
                return ToolResultEvent(id=tool_call.id, name=tool_call.name, error="Permission denied")

        registry = ToolRegistry()
        registry.register(work)
        executor = DenyingExecutor(registry)
        provider = OfflineProvider(
            [[ToolCallEvent(id="a", name="work", arguments={"value": "initial"})], [TextDoneEvent(text="denied")]]
        )
        agent = Agent(
            provider,
            SessionManager(tmp_path / "denied.db"),
            tools=[work],
            plugins=[Transform()],
            tool_executor=executor if constructor_injection else None,
            compactor=None,
        )
        if not constructor_injection:
            agent.tool_executor = DenyingExecutor(agent.tool_registry)
        await collect(agent)
        assert executed == []
        assert audited == [ToolCall("a", "work", {"value": "restricted"})]
        history = await agent.session.get_history("s")
        assert history[1].tool_calls == audited
        assert history[2] == Message(role="tool", tool_call_id="a", name="work", content="Error: Permission denied")
        await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("hook", ["before_run", "before_model", "before_tool", "after_tool", "on_event"])
def test_hook_errors_fail_closed_and_cleanup(tmp_path: Path, hook: str) -> None:
    async def drive() -> None:
        executed: list[str] = []
        cleaned: list[str] = []

        class Failing(AgentPlugin):
            def check(self, name: str) -> None:
                if hook == name:
                    raise RuntimeError(f"failed {name}")

            async def before_run(self, context: RunContext, message: Message) -> Message:
                self.check("before_run")
                return message

            async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
                self.check("before_model")
                return request

            async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
                self.check("before_tool")
                return call

            async def after_tool(self, context: RunContext, result: ToolResultEvent) -> ToolResultEvent:
                self.check("after_tool")
                return result

            async def on_event(self, context: RunContext, event: Event) -> None:
                self.check("on_event")

            async def after_run(self, context: RunContext) -> None:
                cleaned.append("failed plugin")

        async def work() -> str:
            """Do work."""
            executed.append("work")
            return "result"

        provider = OfflineProvider([[ToolCallEvent(id="a", name="work"), ToolCallEvent(id="b", name="work")]])
        agent = Agent(
            provider, SessionManager(tmp_path / "failure.db"), tools=[work], plugins=[Failing()], compactor=None
        )
        with pytest.raises(RuntimeError, match=f"failed {hook}"):
            await collect(agent)
        assert executed == (["work"] if hook == "after_tool" else [])
        assert cleaned == ["failed plugin"]
        if hook == "after_tool":
            history = await agent.session.get_history("s")
            assert [message.tool_call_id for message in history if message.role == "tool"] == ["a", "b"]
            assert "unknown" in str(history[-1].content)
        if hook in {"before_run", "before_model"}:
            assert provider.requests == []
        await agent.close()

    asyncio.run(drive())


def test_all_cleanup_hooks_run_even_if_one_fails(tmp_path: Path) -> None:
    async def drive() -> None:
        cleaned: list[str] = []

        class Cleanup(AgentPlugin):
            async def after_run(self, context: RunContext) -> None:
                cleaned.append("first")
                raise RuntimeError("cleanup failure")

        class Later(AgentPlugin):
            async def after_run(self, context: RunContext) -> None:
                cleaned.append("second")

        agent = Agent(
            OfflineProvider(), SessionManager(tmp_path / "cleanup.db"), plugins=[Cleanup(), Later()], compactor=None
        )
        with pytest.raises(ExceptionGroup, match="cleanup failed") as errors:
            await collect(agent)
        assert str(errors.value.exceptions[0]) == "cleanup failure"
        assert cleaned == ["first", "second"]
        await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("hook", ["before_tool", "after_tool", "executor"])
@pytest.mark.parametrize("field", ["id", "name"])
def test_call_identity_changes_rejected(tmp_path: Path, hook: str, field: str) -> None:
    async def drive() -> None:
        executed: list[str] = []

        class ChangeIdentity(AgentPlugin):
            async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
                if hook == "before_tool":
                    setattr(call, field, "changed")
                return call

            async def after_tool(self, context: RunContext, result: ToolResultEvent) -> ToolResultEvent:
                if hook == "after_tool":
                    setattr(result, field, "changed")
                return result

        class Executor(ToolExecutor):
            async def execute(self, tool_call: ToolCall) -> ToolResultEvent:
                executed.append(tool_call.name)
                result = ToolResultEvent(id=tool_call.id, name=tool_call.name, result="ok")
                if hook == "executor":
                    setattr(result, field, "changed")
                return result

        provider = OfflineProvider([[ToolCallEvent(id="a", name="work")]])
        agent = Agent(provider, SessionManager(tmp_path / "identity.db"), plugins=[ChangeIdentity()], compactor=None)
        agent.tool_executor = Executor(agent.tool_registry)
        with pytest.raises(ValueError, match="preserve tool call id/name"):
            await collect(agent)
        assert executed == ([] if hook == "before_tool" else ["work"])
        history = await agent.session.get_history("s")
        if hook != "before_tool":
            assert history[-1].tool_call_id == "a" and history[-1].name == "work"
        await agent.close()

    asyncio.run(drive())


def test_aclose_persists_result_before_ui_and_repairs_pending_calls(tmp_path: Path) -> None:
    async def drive() -> None:
        executed: list[str] = []
        cleaned: list[bool] = []

        async def work(value: str) -> str:
            """Record execution."""
            executed.append(value)
            return value

        class Cleanup(AgentPlugin):
            async def after_run(self, context: RunContext) -> None:
                cleaned.append(True)

        provider = OfflineProvider(
            [
                [
                    TextDoneEvent(text="explanation"),
                    ToolCallEvent(id="a", name="work", arguments={"value": "first"}),
                    ToolCallEvent(id="b", name="work", arguments={"value": "second"}),
                ]
            ]
        )
        agent = Agent(
            provider, SessionManager(tmp_path / "aclose.db"), tools=[work], plugins=[Cleanup()], compactor=None
        )
        stream = agent.run("go", "s")
        async for event in stream:
            if isinstance(event, ToolResultEvent):
                history = await agent.session.get_history("s")
                assert history[-1].tool_call_id == event.id
                assert history[-1].content == "first"
                await stream.aclose()
                break
        assert executed == ["first"] and cleaned == [True]
        history = await agent.session.get_history("s")
        assert history[1].content == "explanation"
        assert history[-1].tool_call_id == "b" and "unknown" in str(history[-1].content)
        await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("cancel_again", [False, True])
def test_cancel_pending_multi_call_block_repairs_before_return(tmp_path: Path, cancel_again: bool) -> None:
    async def drive() -> None:
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release_cleanup = asyncio.Event()
        executed: list[str] = []
        cleaned: list[bool] = []

        async def work(value: str) -> str:
            """Wait on the second call."""
            executed.append(value)
            if value == "second":
                started.set()
                await asyncio.Event().wait()
            return value

        class Cleanup(AgentPlugin):
            async def after_run(self, context: RunContext) -> None:
                cleaning.set()
                await release_cleanup.wait()
                cleaned.append(True)

        provider = OfflineProvider(
            [
                [
                    ToolCallEvent(id=value, name="work", arguments={"value": value})
                    for value in ("first", "second", "third")
                ]
            ]
        )
        agent = Agent(
            provider, SessionManager(tmp_path / "cancel.db"), tools=[work], plugins=[Cleanup()], compactor=None
        )
        task = asyncio.create_task(collect(agent))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), timeout=5)
        if cancel_again:
            task.cancel()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert cleaned == [True]
        assert executed == ["first", "second"]
        history = await agent.session.get_history("s")
        results = [message for message in history if message.role == "tool"]
        assert [message.tool_call_id for message in results] == ["first", "second", "third"]
        assert results[0].content == "first"
        assert all("unknown" in str(message.content) and "cancelled" in str(message.content) for message in results[1:])
        await agent.close()

    asyncio.run(drive())


def test_crash_resume_repairs_without_replaying_before_new_user(tmp_path: Path) -> None:
    async def drive() -> None:
        db_path = tmp_path / "resume.db"
        session = SessionManager(db_path)
        await session.get_or_create_session("s", "u")
        assistant = Message(
            role="assistant", content="original text", tool_calls=[ToolCall("a", "work"), ToolCall("b", "work")]
        )
        await session.add_message("s", assistant)
        await session.add_message("s", Message(role="tool", tool_call_id="a", name="work", content="completed"))
        executed: list[bool] = []

        async def work() -> str:
            """Must never replay."""
            executed.append(True)
            return "bad"

        provider = OfflineProvider([[TextDoneEvent(text="resumed")], [TextDoneEvent(text="again")]])
        agent = Agent(provider, SessionManager(db_path), tools=[work], compactor=None)
        await collect(agent)
        request = provider.requests[0]
        assert [message.role for message in request.messages] == ["assistant", "tool", "tool", "user"]
        assert request.messages[0] == assistant
        assert request.messages[1].content == "completed"
        assert request.messages[2].tool_call_id == "b" and "unknown" in str(request.messages[2].content)
        await collect(agent, "continue")
        assert executed == []
        assert len([message for message in await agent.session.get_history("s") if message.role == "tool"]) == 2
        await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("cancel_on", ["assistant", "tool"])
def test_cancellation_racing_committed_message_does_not_duplicate_results(tmp_path: Path, cancel_on: str) -> None:
    async def drive() -> None:
        committed = asyncio.Event()
        executed: list[bool] = []

        class PausingSession(SessionManager):
            async def add_message(self, session_id: str, message: Message) -> int:
                row_id = await super().add_message(session_id, message)
                if message.role == cancel_on and not committed.is_set():
                    committed.set()
                    await asyncio.Event().wait()
                return row_id

        async def work() -> str:
            """Record execution before the result commit."""
            executed.append(True)
            return "completed"

        provider = OfflineProvider([[ToolCallEvent(id="a", name="work"), ToolCallEvent(id="b", name="work")]])
        agent = Agent(provider, PausingSession(tmp_path / "commit_race.db"), tools=[work], compactor=None)
        task = asyncio.create_task(collect(agent))
        await asyncio.wait_for(committed.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        history = await agent.session.get_history("s")
        results = [message for message in history if message.role == "tool"]
        assert [message.tool_call_id for message in results] == ["a", "b"]
        assert executed == ([] if cancel_on == "assistant" else [True])
        assert (results[0].content == "completed") is (cancel_on == "tool")
        assert "unknown" in str(results[-1].content)
        await agent.close()

    asyncio.run(drive())


def test_result_notification_failure_keeps_result_and_repairs_rest(tmp_path: Path) -> None:
    async def drive() -> None:
        executed: list[bool] = []

        class FailingObserver(AgentPlugin):
            async def on_event(self, context: RunContext, event: Event) -> None:
                if isinstance(event, ToolResultEvent):
                    raise RuntimeError("notification failed")

        async def work() -> str:
            """Return a real result before notification."""
            executed.append(True)
            return "completed"

        provider = OfflineProvider([[ToolCallEvent(id="a", name="work"), ToolCallEvent(id="b", name="work")]])
        agent = Agent(
            provider,
            SessionManager(tmp_path / "notify_failure.db"),
            tools=[work],
            plugins=[FailingObserver()],
            compactor=None,
        )
        with pytest.raises(RuntimeError, match="notification failed"):
            await collect(agent)
        assert executed == [True]
        history = await agent.session.get_history("s")
        assert history[-2] == Message(role="tool", tool_call_id="a", name="work", content="completed")
        assert history[-1].tool_call_id == "b" and "unknown" in str(history[-1].content)
        await agent.close()

    asyncio.run(drive())


def test_hallucination_error_persisted_and_remaining_calls_repaired(tmp_path: Path) -> None:
    async def drive() -> None:
        provider = OfflineProvider([[ToolCallEvent(id="a", name="missing"), ToolCallEvent(id="b", name="missing")]])
        agent = Agent(
            provider, SessionManager(tmp_path / "hallucination.db"), fail_on_invalid_tool=True, compactor=None
        )
        with pytest.raises(ToolHallucinationError):
            await collect(agent)
        history = await agent.session.get_history("s")
        assert "does not exist" in str(history[-2].content)
        assert history[-2].tool_call_id == "a"
        assert history[-1].tool_call_id == "b" and "unknown" in str(history[-1].content)
        await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("enabled", [False, True])
def test_save_to_switch_controls_schema_extraction_and_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    async def drive() -> None:
        output = tmp_path / "output.txt"

        async def work() -> str:
            """Return local output, accepting no reserved argument."""
            return "tool output"

        def forbidden(*args: object) -> None:
            raise AssertionError("disabled _save_to helper called")

        if not enabled:
            monkeypatch.setattr("nagents.agent._extract_save_path", forbidden)
            monkeypatch.setattr("nagents.agent._save_and_return", forbidden)
            monkeypatch.setattr("nagents.agent._inject_save_to", forbidden)
        provider = OfflineProvider(
            [[ToolCallEvent(id="a", name="work", arguments={"_save_to": str(output)})], [TextDoneEvent(text="done")]]
        )
        agent = Agent(
            provider, SessionManager(tmp_path / "save.db"), tools=[work], save_tool_outputs=enabled, compactor=None
        )
        events = await collect(agent)
        assert ("_save_to" in provider.requests[0].tools[0].parameters.get("properties", {})) is enabled
        assert output.exists() is enabled
        result = next(event for event in events if isinstance(event, ToolResultEvent))
        assert result.error is None
        history = await agent.session.get_history("s")
        if enabled:
            assert output.read_text() == "tool output"
            assert "Tool result saved" in str(history[2].content)
        else:
            assert history[1].tool_calls[0].arguments == {}
            assert history[2].content == "tool output"
        await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("extension", ["plugin", "strategy", "executor", "save"])
def test_unsupported_modes_reject_before_initialization(tmp_path: Path, extension: str) -> None:
    async def drive() -> None:
        provider = OfflineProvider()
        session = SessionManager(tmp_path / "unsupported.db")
        agent = Agent(provider, session)
        if extension == "plugin":
            agent.plugins.append(AgentPlugin())
        elif extension == "strategy":
            agent.compaction_strategy = ReplacementStrategy([Message(role="user", content="replace")])
        elif extension == "executor":
            agent.tool_executor = ToolExecutor(agent.tool_registry)
        else:
            agent.save_tool_outputs = False
        with pytest.raises(ValueError, match="voice mode"):
            _ = [event async for event in agent.run()]
        with pytest.raises(ValueError, match="voice mode"):
            agent.realtime_session()
        agent.batch = True
        with pytest.raises(ValueError, match="batch mode"):
            await collect(agent)
        with pytest.raises(ValueError, match="simple mode"):
            _ = [event async for event in agent.run_simple([Message(role="user", content="go")])]
        with pytest.raises(ValueError, match="batch mode"):
            Agent(provider, session, batch=True, plugins=[AgentPlugin()])
        assert provider.verifications == 0 and provider.requests == []
        assert not session.db_path.exists()
        await agent.close()

    asyncio.run(drive())


def test_provider_error_still_notifies_and_cleans_up(tmp_path: Path) -> None:
    async def drive() -> None:
        observed: list[type[Event]] = []
        cleaned: list[bool] = []

        class Observe(AgentPlugin):
            async def on_event(self, context: RunContext, event: Event) -> None:
                observed.append(type(event))

            async def after_run(self, context: RunContext) -> None:
                cleaned.append(True)

        agent = Agent(
            OfflineProvider([[ErrorEvent(message="offline failure")]]),
            SessionManager(tmp_path / "error.db"),
            plugins=[Observe()],
            compactor=None,
        )
        await collect(agent)
        assert observed == [ErrorEvent, DoneEvent]
        assert cleaned == [True]
        await agent.close()

    asyncio.run(drive())
