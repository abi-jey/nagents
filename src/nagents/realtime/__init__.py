"""
Realtime speech-to-speech integration for nagents.

Provides :class:`RealtimeSession`, a WebSocket client for the OpenAI Realtime
API that runs low-latency voice conversations with speech-to-speech models
(e.g. ``gpt-realtime-2.1``), including automatic tool calling.

The full-duplex audio channel is described by the :class:`AudioInput` and
:class:`AudioOutput` protocols together with :class:`AudioFormat`, which
codifies the audio format/quality the session should use. Concrete file and
in-memory adapters are provided for reference and testing.
"""

from .audio import AudioDuplex
from .audio import AudioFormat
from .audio import AudioInput
from .audio import AudioOutput
from .audio import BytesAudioInput
from .audio import BytesAudioOutput
from .audio import NullAudioOutput
from .audio import WAVFileAudioInput
from .audio import WAVFileAudioOutput
from .client import RealtimeSession
from .types import DEFAULT_REALTIME_MODEL
from .types import REALTIME_VOICES
from .types import RealtimeConfig
from .types import ReasoningEffort
from .types import TurnDetectionType

__all__ = [
    "DEFAULT_REALTIME_MODEL",
    "REALTIME_VOICES",
    "AudioDuplex",
    "AudioFormat",
    "AudioInput",
    "AudioOutput",
    "BytesAudioInput",
    "BytesAudioOutput",
    "NullAudioOutput",
    "RealtimeConfig",
    "RealtimeSession",
    "ReasoningEffort",
    "TurnDetectionType",
    "WAVFileAudioInput",
    "WAVFileAudioOutput",
]
