"""Basic tests for simple_agents package."""

from pathlib import Path
from tempfile import TemporaryDirectory

from simple_agents import Agent
from simple_agents import DoneEvent
from simple_agents import ErrorEvent
from simple_agents import GenerationConfig
from simple_agents import Message
from simple_agents import Provider
from simple_agents import ProviderType
from simple_agents import SessionManager
from simple_agents import TextChunkEvent
from simple_agents import TextDoneEvent
from simple_agents import ToolCallEvent
from simple_agents import ToolDefinition
from simple_agents import ToolResultEvent
from simple_agents import UsageEvent


class TestTypes:
    """Test core type definitions."""

    def test_message_creation(self) -> None:
        """Test Message dataclass creation."""
        msg = Message(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.tool_calls == []
        assert msg.tool_call_id is None

    def test_message_with_tool_calls(self) -> None:
        """Test Message with tool calls."""
        from simple_agents import ToolCall

        tool_call = ToolCall(id="123", name="test_tool", arguments={"arg": "value"})
        msg = Message(role="assistant", content="", tool_calls=[tool_call])
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "test_tool"

    def test_tool_definition(self) -> None:
        """Test ToolDefinition creation."""
        tool = ToolDefinition(
            name="test_tool",
            description="A test tool",
            parameters={
                "type": "object",
                "properties": {"arg": {"type": "string"}},
            },
        )
        assert tool.name == "test_tool"
        assert tool.description == "A test tool"

    def test_generation_config(self) -> None:
        """Test GenerationConfig creation."""
        config = GenerationConfig(
            temperature=0.7,
            max_tokens=1000,
            top_p=0.9,
        )
        assert config.temperature == 0.7
        assert config.max_tokens == 1000
        assert config.top_p == 0.9


class TestEvents:
    """Test event types."""

    def test_text_chunk_event(self) -> None:
        """Test TextChunkEvent creation."""
        event = TextChunkEvent(chunk="Hello")
        assert event.chunk == "Hello"

    def test_text_done_event(self) -> None:
        """Test TextDoneEvent creation."""
        event = TextDoneEvent(text="Complete message")
        assert event.text == "Complete message"

    def test_tool_call_event(self) -> None:
        """Test ToolCallEvent creation."""
        event = ToolCallEvent(
            id="123",
            name="test_tool",
            arguments={"arg": "value"},
        )
        assert event.id == "123"
        assert event.name == "test_tool"
        assert event.arguments == {"arg": "value"}

    def test_tool_result_event(self) -> None:
        """Test ToolResultEvent creation."""
        event = ToolResultEvent(
            id="123",
            name="test_tool",
            result="success",
            duration_ms=10.5,
        )
        assert event.id == "123"
        assert event.result == "success"
        assert event.duration_ms == 10.5
        assert event.error is None

    def test_usage_event(self) -> None:
        """Test UsageEvent creation."""
        event = UsageEvent(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
        )
        assert event.prompt_tokens == 100
        assert event.completion_tokens == 50
        assert event.total_tokens == 150

    def test_error_event(self) -> None:
        """Test ErrorEvent creation."""
        event = ErrorEvent(
            message="Something went wrong",
            code="ERR001",
            recoverable=True,
        )
        assert event.message == "Something went wrong"
        assert event.code == "ERR001"
        assert event.recoverable is True

    def test_done_event(self) -> None:
        """Test DoneEvent creation."""
        event = DoneEvent(
            final_text="Complete response",
            session_id="session-123",
        )
        assert event.final_text == "Complete response"
        assert event.session_id == "session-123"


class TestProvider:
    """Test Provider configuration."""

    def test_provider_creation_openai(self) -> None:
        """Test OpenAI provider creation."""
        provider = Provider(
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            api_key="test-key",
            model="gpt-4o-mini",
        )
        assert provider.provider_type == ProviderType.OPENAI_COMPATIBLE
        assert provider.model == "gpt-4o-mini"

    def test_provider_creation_gemini(self) -> None:
        """Test Gemini provider creation."""
        provider = Provider(
            provider_type=ProviderType.GEMINI_NATIVE,
            api_key="test-key",
            model="gemini-2.0-flash",
        )
        assert provider.provider_type == ProviderType.GEMINI_NATIVE
        assert provider.model == "gemini-2.0-flash"

    def test_provider_creation_anthropic(self) -> None:
        """Test Anthropic provider creation."""
        provider = Provider(
            provider_type=ProviderType.ANTHROPIC,
            api_key="test-key",
            model="claude-3-5-sonnet-20241022",
        )
        assert provider.provider_type == ProviderType.ANTHROPIC
        assert provider.model == "claude-3-5-sonnet-20241022"


class TestAgent:
    """Test Agent class."""

    def test_agent_creation(self) -> None:
        """Test Agent instantiation."""
        with TemporaryDirectory() as tmpdir:
            provider = Provider(
                provider_type=ProviderType.OPENAI_COMPATIBLE,
                api_key="test-key",
                model="gpt-4o-mini",
            )
            session_manager = SessionManager(Path(tmpdir) / "sessions.db")
            agent = Agent(provider=provider, session_manager=session_manager)
            assert agent.provider == provider

    def test_agent_with_tools(self) -> None:
        """Test Agent with tool functions."""

        def example_tool(arg: str) -> str:
            """An example tool."""
            return f"Result: {arg}"

        with TemporaryDirectory() as tmpdir:
            provider = Provider(
                provider_type=ProviderType.OPENAI_COMPATIBLE,
                api_key="test-key",
                model="gpt-4o-mini",
            )
            session_manager = SessionManager(Path(tmpdir) / "sessions.db")
            agent = Agent(
                provider=provider,
                session_manager=session_manager,
                tools=[example_tool],
            )
            # Check tool was registered
            assert agent.tool_registry is not None

    def test_agent_with_system_prompt(self) -> None:
        """Test Agent with system prompt."""
        with TemporaryDirectory() as tmpdir:
            provider = Provider(
                provider_type=ProviderType.OPENAI_COMPATIBLE,
                api_key="test-key",
                model="gpt-4o-mini",
            )
            session_manager = SessionManager(Path(tmpdir) / "sessions.db")
            agent = Agent(
                provider=provider,
                session_manager=session_manager,
                system_prompt="You are a helpful assistant.",
            )
            assert agent.system_prompt == "You are a helpful assistant."
