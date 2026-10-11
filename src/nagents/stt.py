"""
Speech-to-Text (STT) services for audio transcription.

Provides an abstract interface and implementations for various STT providers.
"""

import base64
import logging
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Any

from .http import HTTPClient
from .media import AUDIO_MIME_TYPES

logger = logging.getLogger(__name__)


def detect_audio_format(file_path: Path | str) -> str:
    """
    Detect audio format from file extension.

    Args:
        file_path: Path to the audio file

    Returns:
        Audio format string (wav, mp3, etc.)

    Raises:
        ValueError: If format is not supported
    """
    path = Path(file_path)
    ext = path.suffix.lower().lstrip(".")

    if ext in AUDIO_MIME_TYPES:
        return ext
    raise ValueError(f"Unsupported audio format: {ext}. Supported: {list(AUDIO_MIME_TYPES.keys())}")


def get_mime_type(format: str) -> str:
    """
    Get MIME type for audio format.

    Args:
        format: Audio format (wav, mp3, etc.)

    Returns:
        MIME type string
    """
    if format in AUDIO_MIME_TYPES:
        return AUDIO_MIME_TYPES[format]
    return f"audio/{format}"


class TranscriptionResult:
    """Result of audio transcription."""

    def __init__(
        self,
        text: str,
        language: str | None = None,
        duration: float | None = None,
        extra: dict[str, Any] | None = None,
    ):
        self.text = text
        self.language = language
        self.duration = duration
        self.extra = extra or {}


class STTService(ABC):
    """Abstract base class for Speech-to-Text services."""

    @abstractmethod
    async def transcribe(
        self,
        audio_data: bytes,
        format: str = "wav",
        language: str | None = None,
    ) -> TranscriptionResult:
        """
        Transcribe audio data to text.

        Args:
            audio_data: Raw audio bytes
            format: Audio format (wav, mp3, ogg, etc.)
            language: Optional language hint (e.g., "en-US")

        Returns:
            TranscriptionResult with transcribed text
        """
        pass

    async def transcribe_file(
        self,
        file_path: Path | str,
        language: str | None = None,
    ) -> TranscriptionResult:
        """
        Transcribe audio from file.

        Args:
            file_path: Path to audio file
            language: Optional language hint

        Returns:
            TranscriptionResult with transcribed text
        """
        path = Path(file_path)
        format = detect_audio_format(path)
        audio_data = path.read_bytes()
        return await self.transcribe(audio_data, format, language)

    async def transcribe_base64(
        self,
        base64_data: str,
        format: str = "wav",
        language: str | None = None,
    ) -> TranscriptionResult:
        """
        Transcribe base64-encoded audio data.

        Args:
            base64_data: Base64-encoded audio data
            format: Audio format
            language: Optional language hint

        Returns:
            TranscriptionResult with transcribed text
        """
        audio_data = base64.b64decode(base64_data)
        return await self.transcribe(audio_data, format, language)

    @abstractmethod
    async def close(self) -> None:
        """Close the STT service and release resources."""
        ...


# Supported input formats for OpenAI's /v1/audio/transcriptions endpoint.
# See: https://developers.openai.com/api/docs/guides/speech-to-text
OPENAI_TRANSCRIPTION_FORMATS: frozenset[str] = frozenset(
    {"mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm", "flac", "ogg"}
)


class OpenAISTTService(STTService):
    """
    OpenAI Speech-to-Text service.

    Uses OpenAI's /v1/audio/transcriptions endpoint for audio transcription.
    Supports: mp3, mp4, mpeg, mpga, m4a, wav, webm, flac, ogg (max 25 MB).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-transcribe",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        language: str = "",
        prompt: str = "",
    ):
        """
        Initialize OpenAI STT service.

        Args:
            api_key: OpenAI API key
            model: Transcription model (default: gpt-4o-transcribe).
                   Options: gpt-4o-transcribe, gpt-4o-mini-transcribe, whisper-1
            base_url: API base URL (default: https://api.openai.com/v1)
            timeout: Request timeout in seconds
            language: Optional language hint in ISO-639-1 format (e.g. "en")
            prompt: Optional prompt to guide transcription style
        """
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.language = language
        self.prompt = prompt
        self._http = HTTPClient(timeout=timeout)

    async def transcribe(
        self,
        audio_data: bytes,
        format: str = "wav",
        language: str | None = None,
    ) -> TranscriptionResult:
        """
        Transcribe audio using OpenAI's transcription endpoint.

        Args:
            audio_data: Raw audio bytes
            format: Audio format (wav, mp3, ogg, m4a, etc.)
            language: Optional language hint in ISO-639-1 (e.g. "en").
                      Overrides the instance-level language if provided.

        Returns:
            TranscriptionResult with transcribed text

        Raises:
            ValueError: If audio format is not supported by OpenAI
        """
        if format not in OPENAI_TRANSCRIPTION_FORMATS:
            raise ValueError(
                f"Audio format '{format}' is not supported by OpenAI transcription. "
                f"Supported: {sorted(OPENAI_TRANSCRIPTION_FORMATS)}"
            )

        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        mime_type = get_mime_type(format)
        filename = f"audio.{format}"

        fields: dict[str, str | tuple[str, bytes, str]] = {
            "file": (filename, audio_data, mime_type),
            "model": self.model,
            "response_format": "json",
        }

        lang = language or self.language
        if lang:
            fields["language"] = lang
        if self.prompt:
            fields["prompt"] = self.prompt

        response = await self._http.post_multipart(url, fields, headers)

        text = response.get("text", "")
        usage = response.get("usage", {})

        return TranscriptionResult(
            text=text.strip() if isinstance(text, str) else "",
            language=lang or None,
            extra={
                "model": self.model,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.close()


class GeminiSTTService(STTService):
    """
    Gemini Speech-to-Text service.

    Uses Gemini's native audio transcription capabilities.
    Supports multiple audio formats including Telegram voice messages (ogg/oga).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        timeout: float = 60.0,
    ):
        """
        Initialize Gemini STT service.

        Args:
            api_key: Gemini API key
            model: Gemini model to use (default: gemini-2.0-flash)
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._http = HTTPClient(timeout=timeout)
        self._base_url = "https://generativelanguage.googleapis.com/v1beta"

    async def transcribe(
        self,
        audio_data: bytes,
        format: str = "wav",
        language: str | None = None,
    ) -> TranscriptionResult:
        """
        Transcribe audio using Gemini.

        Args:
            audio_data: Raw audio bytes
            format: Audio format (wav, mp3, ogg, oga, etc.)
            language: Optional language hint (not used by Gemini, auto-detected)

        Returns:
            TranscriptionResult with transcribed text
        """
        mime_type = get_mime_type(format)
        base64_audio = base64.b64encode(audio_data).decode("utf-8")

        url = f"{self._base_url}/models/{self.model}:generateContent?key={self.api_key}"

        body: dict[str, Any] = {
            "contents": [
                {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": base64_audio,
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {
                "maxOutputTokens": 8192,
            },
        }

        response = await self._http.post_json(url, body, {})

        text = ""
        candidates = response.get("candidates", [])
        if candidates:
            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            for part in parts:
                if "text" in part:
                    text += part["text"]

        usage = response.get("usageMetadata", {})

        return TranscriptionResult(
            text=text.strip(),
            language=language,
            extra={
                "model": self.model,
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
            },
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.close()
