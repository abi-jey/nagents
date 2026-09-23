"""Compatibility exports for the shared audio primitives."""

from ..audio import AudioDuplex
from ..audio import AudioFormat
from ..audio import AudioInput
from ..audio import AudioOutput
from ..audio import BytesAudioInput
from ..audio import BytesAudioOutput
from ..audio import NullAudioOutput
from ..audio import WAVFileAudioInput
from ..audio import WAVFileAudioOutput

__all__ = [
    "AudioDuplex",
    "AudioFormat",
    "AudioInput",
    "AudioOutput",
    "BytesAudioInput",
    "BytesAudioOutput",
    "NullAudioOutput",
    "WAVFileAudioInput",
    "WAVFileAudioOutput",
]
