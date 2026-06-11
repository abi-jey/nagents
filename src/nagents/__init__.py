"""
homelab_agent v2 - Direct HTTP-based LLM integration.

This module provides a clean, dependency-free implementation for
communicating with LLM providers directly via HTTP. It supports:

- OpenAI API (and compatible: Gemini via compat, OpenRouter, Ollama, etc.)
- Gemini Native REST API
- Anthropic Claude API

Key features:
- No SDK dependencies (just aiohttp and aiosqlite)
- Event-based streaming responses
- Automatic tool execution with event emission
- SQLite-based session management
- Auto-initialization with model verification on first run()
- Multimodal support (images, audio, documents)
- Batch processing with 50% cost discount (OpenAI, Anthropic)
- Persistent batch jobs that survive agent restarts

Example:
    from homelab_agent.v2 import Provider, ProviderType, Agent, SessionManager
    from pathlib import Path

    # Create provider
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key="sk-...",
        model="gpt-4o",
    )

    # Create session manager
    session = SessionManager(Path("sessions.db"))

    # Create agent with tools
    def get_weather(city: str) -> str:
        '''Get the current weather for a city.'''
        return f"Weather in {city}: Sunny, 22C"

    agent = Agent(
        provider=provider,
        session_manager=session,
        tools=[get_weather],
        system_prompt="You are a helpful assistant.",
    )

    # Just use it - auto-initializes on first run()
    async for event in agent.run("What's the weather in Paris?"):
        if isinstance(event, TextChunkEvent):
            print(event.chunk, end="", flush=True)
        elif isinstance(event, ToolCallEvent):
            print(f"\\nCalling: {event.name}({event.arguments})")
        elif isinstance(event, ToolResultEvent):
            print(f"Result: {event.result}")
        elif isinstance(event, DoneEvent):
            print(f"\\nDone (session: {event.session_id})")
            # Usage is always available on all events (never None)
            print(f"Tokens this generation: {event.usage.total_tokens}")
            if event.usage.session:
                print(f"Total session tokens: {event.usage.session.total_tokens}")

    # Don't forget to close when done
    await agent.close()

Batch Processing with Persistence:
    from homelab_agent.v2 import BatchManager, BatchRequest, ProviderType, Message
    from pathlib import Path

    # BatchManager persists jobs to SQLite, survives restarts
    manager = BatchManager(
        api_key="sk-...",
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        model="gpt-4o-mini",
        db_path=Path("~/.homelab/batches.db"),
    )

    # Register callback handler for results
    @manager.register_callback("summarize")
    async def handle_summaries(results, context):
        for result in results:
            print(f"{result.custom_id}: {result.content}")

    # Create batch with callback
    requests = [
        BatchRequest(custom_id="r1", messages=[Message(role="user", content="Hi")]),
        BatchRequest(custom_id="r2", messages=[Message(role="user", content="Hello")]),
    ]
    job = await manager.create_batch(
        requests,
        callback_name="summarize",
        callback_context={"source": "reports"},
    )

    # On agent restart, resume pending batches
    await manager.resume_all()
"""

from .agent import Agent
from .agent import UnsupportedAudioBehavior
from .agent import UnsupportedAudioError
from .batch import BatchClient
from .batch import BatchConfig
from .batch import BatchJob
from .batch import BatchManager
from .batch import BatchRequest
from .batch import BatchRequestCounts
from .batch import BatchResult
from .batch import BatchStatus
from .batch import BatchStore
from .batch import BatchToolCall
from .batch import ToolCallFunction
from .batch import UsageInfo
from .compaction import estimate_messages_tokens
from .compaction import get_model_context_limit
from .compaction import truncate_tool_results
from .compactor import DEFAULT_COMPACTOR
from .compactor import DEFAULT_COMPACT_PROMPT
from .compactor import Compactor
from .compactor import Messages
from .compactor import Tokens
from .events import CompactionDoneEvent
from .events import CompactionStartedEvent
from .events import DoneEvent
from .events import ErrorEvent
from .events import Event
from .events import EventType
from .events import FinishReason
from .events import RateLimitEvent
from .events import ReasoningChunkEvent
from .events import TextChunkEvent
from .events import TextDoneEvent
from .events import TokenUsage
from .events import ToolCallEvent
from .events import ToolResultEvent
from .events import ToolResultType
from .events import Usage
from .exceptions import NagentsError
from .exceptions import ToolHallucinationError
from .mcp import MCPClient
from .mcp import MCPError
from .mcp import MCPManager
from .mcp import MCPServerConfig
from .http import FileHTTPLogger
from .media import AUDIO_MIME_TYPES
from .media import DOCUMENT_MIME_TYPES
from .media import IMAGE_MIME_TYPES
from .media import MediaCapabilities
from .media import get_media_capabilities
from .media import transcode_audio_to_wav
from .provider import Provider
from .provider import ProviderType
from .session import SessionManager
from .stt import OPENAI_TRANSCRIPTION_FORMATS
from .stt import GeminiSTTService
from .stt import OpenAISTTService
from .stt import STTService
from .stt import TranscriptionResult
from .tools import ToolExecutor
from .tools import ToolRegistry
from .types import AudioContent
from .types import ContentPart
from .types import DocumentContent
from .types import GeminiThinkingConfig
from .types import GenerationConfig
from .types import ImageContent
from .types import JsonSchema
from .types import JsonSchemaProperty
from .types import JsonValue
from .types import Message
from .types import OpenRouterReasoningConfig
from .types import RetryConfig
from .types import TextContent
from .types import ToolArguments
from .types import ToolCall
from .types import ToolDefinition

__all__ = [
    "AUDIO_MIME_TYPES",
    "DEFAULT_COMPACTOR",
    "DEFAULT_COMPACT_PROMPT",
    "DOCUMENT_MIME_TYPES",
    "IMAGE_MIME_TYPES",
    "OPENAI_TRANSCRIPTION_FORMATS",
    "Agent",
    "AudioContent",
    "BatchClient",
    "BatchConfig",
    "BatchJob",
    "BatchManager",
    "BatchRequest",
    "BatchRequestCounts",
    "BatchResult",
    "BatchStatus",
    "BatchStore",
    "BatchToolCall",
    "CompactionDoneEvent",
    "CompactionStartedEvent",
    "Compactor",
    "ContentPart",
    "DocumentContent",
    "DoneEvent",
    "ErrorEvent",
    "Event",
    "EventType",
    "FileHTTPLogger",
    "FinishReason",
    "GeminiSTTService",
    "GeminiThinkingConfig",
    "GenerationConfig",
    "ImageContent",
    "JsonSchema",
    "JsonSchemaProperty",
    "JsonValue",
    "MCPClient",
    "MCPError",
    "MCPManager",
    "MCPServerConfig",
    "MediaCapabilities",
    "Message",
    "Messages",
    "NagentsError",
    "OpenAISTTService",
    "OpenRouterReasoningConfig",
    "Provider",
    "ProviderType",
    "RateLimitEvent",
    "ReasoningChunkEvent",
    "RetryConfig",
    "STTService",
    "SessionManager",
    "TextChunkEvent",
    "TextContent",
    "TextDoneEvent",
    "TokenUsage",
    "Tokens",
    "ToolArguments",
    "ToolCall",
    "ToolCallEvent",
    "ToolCallFunction",
    "ToolDefinition",
    "ToolExecutor",
    "ToolHallucinationError",
    "ToolRegistry",
    "ToolResultEvent",
    "ToolResultType",
    "TranscriptionResult",
    "UnsupportedAudioBehavior",
    "UnsupportedAudioError",
    "Usage",
    "UsageInfo",
    "estimate_messages_tokens",
    "get_media_capabilities",
    "get_model_context_limit",
    "transcode_audio_to_wav",
    "truncate_tool_results",
]
