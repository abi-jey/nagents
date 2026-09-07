"""
Configuration types for the Realtime speech-to-speech API.

These types map onto the OpenAI Realtime API session schema. They are kept
independent of the wire protocol so they can be built, inspected, and tested
without an open connection.
"""

from dataclasses import dataclass
from dataclasses import field
from typing import Literal

# Default Realtime model used when none is configured.
DEFAULT_REALTIME_MODEL = "gpt-realtime-2.1"

# Built-in voices available to Realtime models.
REALTIME_VOICES: tuple[str, ...] = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
)

# Reasoning effort levels supported by reasoning-capable Realtime models
# (e.g. gpt-realtime-2, gpt-realtime-2.1).
ReasoningEffort = Literal["minimal", "low", "medium", "high"]

# Turn-detection (VAD) strategies accepted by the Realtime API.
TurnDetectionType = Literal["server_vad", "semantic_vad"]


@dataclass
class RealtimeConfig:
    """
    Configuration for a Realtime speech-to-speech session.

    Attributes:
        model: The Realtime model to use (e.g. "gpt-realtime-2.1"). ``None``
            means "inherit from the provider"; resolved to
            :data:`DEFAULT_REALTIME_MODEL` if still unset.
        instructions: System instructions guiding the model's behavior.
        voice: The voice used for audio output.
        output_modalities: Response modalities. ``["audio"]`` (default) yields
            audio plus a transcript; ``["text"]`` yields text only.
        input_audio_format: Input audio codec ("audio/pcm", "audio/pcmu", ...).
        input_audio_rate: Input audio sample rate in Hz.
        output_audio_format: Output audio codec.
        output_audio_rate: Output audio sample rate in Hz.
        turn_detection: Whether voice activity detection is enabled. When
            disabled, the client must commit audio and create responses
            manually (push-to-talk style).
        turn_detection_type: VAD strategy when turn detection is enabled.
        input_transcription_model: When set, enables server-side transcription
            of user audio (e.g. "gpt-4o-mini-transcribe"), emitting
            input-transcript events for the recognized user speech.
        max_output_tokens: Cap on output tokens per response (1-4096 or None).
        reasoning_effort: Reasoning effort for reasoning-capable models.
        tool_choice: How the model chooses tools ("auto", "none", "required").
        parallel_tool_calls: Whether multiple tools may be called in parallel.
    """

    model: str | None = None
    instructions: str = ""
    voice: str = "marin"
    output_modalities: list[str] = field(default_factory=lambda: ["audio"])
    input_audio_format: str = "audio/pcm"
    input_audio_rate: int = 24000
    output_audio_format: str = "audio/pcm"
    output_audio_rate: int = 24000
    turn_detection: bool = True
    turn_detection_type: TurnDetectionType = "server_vad"
    input_transcription_model: str | None = None
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffort | None = None
    tool_choice: str = "auto"
    parallel_tool_calls: bool | None = None
