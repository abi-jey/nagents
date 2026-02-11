"""
Batch processing types for the v2 LLM integration module.

Supports batch processing for OpenAI and Anthropic APIs.
Both providers offer 50% cost discount for batch processing.
"""

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any
from typing import Literal

from ..types import GenerationConfig
from ..types import Message
from ..types import ToolDefinition


class BatchStatus(Enum):
    """Status of a batch job."""

    # Common statuses
    VALIDATING = "validating"  # Input being validated
    IN_PROGRESS = "in_progress"  # Batch is processing
    COMPLETED = "completed"  # All requests completed
    FAILED = "failed"  # Batch failed validation or processing
    EXPIRED = "expired"  # Batch didn't complete in time window
    CANCELLING = "cancelling"  # Cancellation in progress
    CANCELLED = "cancelled"  # Batch was cancelled

    # OpenAI-specific
    FINALIZING = "finalizing"  # Results being prepared

    # Anthropic-specific (mapped to common)
    ENDED = "ended"  # Processing ended (success or partial)


@dataclass
class BatchRequest:
    """
    A single request in a batch.

    This is the common format used to build batch requests.
    Adapters convert this to provider-specific formats.
    """

    custom_id: str  # Unique ID to match request with result
    messages: list[Message]
    model: str | None = None  # Optional override (uses batch default if not set)
    tools: list[ToolDefinition] | None = None
    config: GenerationConfig | None = None
    system_prompt: str | None = None  # Convenience for system message


@dataclass
class BatchRequestCounts:
    """Count of requests in various states."""

    total: int = 0
    processing: int = 0  # Anthropic
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    expired: int = 0


@dataclass
class BatchJob:
    """
    Represents a batch processing job.

    Contains metadata about the batch and its current status.
    """

    id: str
    status: BatchStatus
    provider: Literal["openai", "anthropic"]

    # Request counts
    request_counts: BatchRequestCounts = field(default_factory=BatchRequestCounts)

    # Timestamps (ISO format)
    created_at: str | None = None
    expires_at: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None

    # OpenAI-specific
    input_file_id: str | None = None
    output_file_id: str | None = None
    error_file_id: str | None = None
    endpoint: str | None = None

    # Anthropic-specific
    results_url: str | None = None

    # Metadata
    metadata: dict[str, str] = field(default_factory=dict)

    # Raw response for debugging
    raw_response: dict[str, Any] | None = None


@dataclass
class BatchResult:
    """
    Result for a single request in a batch.

    The result can be one of:
    - succeeded: Contains the response message/content
    - errored: Contains error information
    - cancelled: Request was cancelled before processing
    - expired: Batch expired before request was processed
    """

    custom_id: str
    result_type: Literal["succeeded", "errored", "cancelled", "expired"]

    # For succeeded results
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    usage: dict[str, int] | None = None

    # For errored results
    error_type: str | None = None
    error_message: str | None = None

    # Full response for advanced use cases
    raw_response: dict[str, Any] | None = None


@dataclass
class BatchConfig:
    """
    Configuration for batch processing.

    OpenAI:
    - endpoint: /v1/chat/completions, /v1/embeddings, etc.
    - completion_window: Only "24h" supported currently

    Anthropic:
    - No additional config needed (uses Messages API format)
    """

    # OpenAI-specific
    endpoint: str = "/v1/chat/completions"
    completion_window: str = "24h"

    # Common
    metadata: dict[str, str] | None = None
