"""
Media format support and capabilities for LLM providers.

Provides:
- MIME type registries for audio, image, and document formats
- Provider/model-aware media capability resolution
- Automatic audio transcoding via ffmpeg subprocess
"""

from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

logger = logging.getLogger(__name__)


# =============================================================================
# MIME Type Registries
# =============================================================================

AUDIO_MIME_TYPES: dict[str, str] = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "webm": "audio/webm",
    "aac": "audio/aac",
    "aiff": "audio/aiff",
    "pcm": "audio/L16",
}

IMAGE_MIME_TYPES: dict[str, str] = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
}

DOCUMENT_MIME_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
}


def get_mime_type(format_str: str, category: str = "audio") -> str:
    """
    Get the MIME type for a given format string and category.

    Args:
        format_str: Format identifier (e.g. "wav", "mp3", "jpeg", "pdf")
        category: One of "audio", "image", "document"

    Returns:
        MIME type string (e.g. "audio/wav", "image/jpeg")
    """
    registries: dict[str, dict[str, str]] = {
        "audio": AUDIO_MIME_TYPES,
        "image": IMAGE_MIME_TYPES,
        "document": DOCUMENT_MIME_TYPES,
    }
    registry = registries.get(category, {})
    if format_str in registry:
        return registry[format_str]
    # Fallback: construct from category/format
    return f"{category}/{format_str}"


# =============================================================================
# Media Capabilities
# =============================================================================


@dataclass(frozen=True)
class MediaCapabilities:
    """
    Describes which media formats a provider+model combination supports.

    Each field is a frozenset of format strings (e.g. {"wav", "mp3"}).
    An empty frozenset means the media type is not supported at all.
    """

    audio_formats: frozenset[str] = field(default_factory=frozenset)
    image_formats: frozenset[str] = field(default_factory=frozenset)
    document_formats: frozenset[str] = field(default_factory=frozenset)

    def supports_audio(self, fmt: str) -> bool:
        """Check if a specific audio format is supported."""
        return fmt in self.audio_formats

    def supports_image(self, media_type: str) -> bool:
        """Check if a specific image media type is supported (by format key)."""
        return media_type in self.image_formats

    def supports_document(self, media_type: str) -> bool:
        """Check if a specific document media type is supported (by format key)."""
        return media_type in self.document_formats


# Provider-level defaults keyed by ProviderType.value strings.
# Using string keys avoids a circular import with provider.base.
_PROVIDER_CAPABILITIES: dict[str, MediaCapabilities] = {
    # Most OpenAI models do NOT support input_audio content blocks.
    # Only audio-specific models (gpt-4o-audio-preview, gpt-audio-*)
    # accept them. Default to no audio support; model overrides add it.
    "openai_compatible": MediaCapabilities(
        audio_formats=frozenset(),
        image_formats=frozenset({"jpeg", "jpg", "png", "gif", "webp"}),
        document_formats=frozenset(),
    ),
    "openrouter": MediaCapabilities(
        audio_formats=frozenset(),
        image_formats=frozenset({"jpeg", "jpg", "png", "gif", "webp"}),
        document_formats=frozenset(),
    ),
    # Azure OpenAI V1 does NOT support input_audio content blocks at all.
    "azure_openai_compatible_v1": MediaCapabilities(
        audio_formats=frozenset(),
        image_formats=frozenset({"jpeg", "jpg", "png", "gif", "webp"}),
        document_formats=frozenset(),
    ),
    "azure_openai_compatible": MediaCapabilities(
        audio_formats=frozenset(),
        image_formats=frozenset({"jpeg", "jpg", "png", "gif", "webp"}),
        document_formats=frozenset(),
    ),
    "anthropic": MediaCapabilities(
        audio_formats=frozenset(),
        image_formats=frozenset({"jpeg", "jpg", "png", "gif", "webp"}),
        document_formats=frozenset({"pdf"}),
    ),
    "gemini_native": MediaCapabilities(
        audio_formats=frozenset({"wav", "mp3", "aiff", "aac", "ogg", "flac"}),
        image_formats=frozenset({"jpeg", "jpg", "png", "webp", "heic", "heif"}),
        document_formats=frozenset({"pdf"}),
    ),
}

# Model-level overrides (take precedence over provider defaults).
# Entries: (provider_type_value, model_name_prefix, capabilities)
# Checked in order — first prefix match wins.
_MODEL_CAPABILITIES: list[tuple[str, str, MediaCapabilities]] = [
    # -------------------------------------------------------------------------
    # Gemini models — comprehensive entries
    # -------------------------------------------------------------------------
    # Gemini 2.5 series (latest, full multimodal)
    (
        "gemini_native",
        "gemini-2.5-pro",
        MediaCapabilities(
            audio_formats=frozenset({"wav", "mp3", "aiff", "aac", "ogg", "flac"}),
            image_formats=frozenset({"jpeg", "jpg", "png", "webp", "heic", "heif"}),
            document_formats=frozenset({"pdf"}),
        ),
    ),
    (
        "gemini_native",
        "gemini-2.5-flash",
        MediaCapabilities(
            audio_formats=frozenset({"wav", "mp3", "aiff", "aac", "ogg", "flac"}),
            image_formats=frozenset({"jpeg", "jpg", "png", "webp", "heic", "heif"}),
            document_formats=frozenset({"pdf"}),
        ),
    ),
    # Gemini 2.0 series
    (
        "gemini_native",
        "gemini-2.0-flash",
        MediaCapabilities(
            audio_formats=frozenset({"wav", "mp3", "aiff", "aac", "ogg", "flac"}),
            image_formats=frozenset({"jpeg", "jpg", "png", "webp", "heic", "heif"}),
            document_formats=frozenset({"pdf"}),
        ),
    ),
    (
        "gemini_native",
        "gemini-2.0-pro",
        MediaCapabilities(
            audio_formats=frozenset({"wav", "mp3", "aiff", "aac", "ogg", "flac"}),
            image_formats=frozenset({"jpeg", "jpg", "png", "webp", "heic", "heif"}),
            document_formats=frozenset({"pdf"}),
        ),
    ),
    # Gemini 1.5 series
    (
        "gemini_native",
        "gemini-1.5-pro",
        MediaCapabilities(
            audio_formats=frozenset({"wav", "mp3", "aiff", "aac", "ogg", "flac"}),
            image_formats=frozenset({"jpeg", "jpg", "png", "webp", "heic", "heif"}),
            document_formats=frozenset({"pdf"}),
        ),
    ),
    (
        "gemini_native",
        "gemini-1.5-flash",
        MediaCapabilities(
            audio_formats=frozenset({"wav", "mp3", "aiff", "aac", "ogg", "flac"}),
            image_formats=frozenset({"jpeg", "jpg", "png", "webp", "heic", "heif"}),
            document_formats=frozenset({"pdf"}),
        ),
    ),
    # -------------------------------------------------------------------------
    # OpenAI models — only audio-specific models support input_audio blocks
    # -------------------------------------------------------------------------
    # gpt-4o-audio-preview — supports inline audio input
    (
        "openai_compatible",
        "gpt-4o-audio-preview",
        MediaCapabilities(
            audio_formats=frozenset({"wav", "mp3"}),
            image_formats=frozenset({"jpeg", "jpg", "png", "gif", "webp"}),
            document_formats=frozenset(),
        ),
    ),
    # gpt-4o-mini-audio-preview — supports inline audio input
    (
        "openai_compatible",
        "gpt-4o-mini-audio-preview",
        MediaCapabilities(
            audio_formats=frozenset({"wav", "mp3"}),
            image_formats=frozenset({"jpeg", "jpg", "png", "gif", "webp"}),
            document_formats=frozenset(),
        ),
    ),
    # gpt-audio-* family — supports inline audio input
    (
        "openai_compatible",
        "gpt-audio",
        MediaCapabilities(
            audio_formats=frozenset({"wav", "mp3"}),
            image_formats=frozenset({"jpeg", "jpg", "png", "gif", "webp"}),
            document_formats=frozenset(),
        ),
    ),
]


def get_media_capabilities(provider_type_value: str, model: str) -> MediaCapabilities:
    """
    Resolve media capabilities for a provider+model combination.

    Model-level overrides take precedence over provider defaults.
    Uses prefix matching for model names (e.g. "gpt-4o" matches "gpt-4o-2024-08-06").

    Args:
        provider_type_value: The provider type enum value string
                             (e.g. "openai_compatible", "gemini_native")
        model: The model identifier

    Returns:
        MediaCapabilities for the provider+model combination
    """
    # Check model-level overrides (first prefix match wins)
    for cap_provider, cap_prefix, capabilities in _MODEL_CAPABILITIES:
        if cap_provider == provider_type_value and model.startswith(cap_prefix):
            return capabilities

    # Fall back to provider-level defaults
    return _PROVIDER_CAPABILITIES.get(
        provider_type_value,
        MediaCapabilities(),  # Empty capabilities if provider is unknown
    )


# =============================================================================
# Audio Transcoding
# =============================================================================


async def transcode_audio_to_wav(base64_data: str, source_format: str) -> str:
    """
    Transcode audio data from any format to WAV using ffmpeg.

    Uses asyncio subprocess to avoid blocking the event loop.
    Writes to temp files for ffmpeg input/output.

    Args:
        base64_data: Base64-encoded source audio data
        source_format: Source audio format (e.g. "ogg", "m4a", "flac")

    Returns:
        Base64-encoded WAV audio data

    Raises:
        RuntimeError: If ffmpeg is not available or transcoding fails
    """
    raw_data = base64.b64decode(base64_data)

    # Use temp directory for input/output files
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        input_file = tmp_path / f"input.{source_format}"
        output_file = tmp_path / "output.wav"

        # Write input data
        input_file.write_bytes(raw_data)

        # Run ffmpeg: convert to WAV (PCM 16-bit, mono, 16kHz — widely compatible)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-i",
            str(input_file),
            "-ar",
            "16000",  # 16kHz sample rate
            "-ac",
            "1",  # mono
            "-sample_fmt",
            "s16",  # 16-bit PCM
            "-f",
            "wav",
            "-y",  # overwrite output
            str(output_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg transcoding failed (exit code {process.returncode}): {error_msg}")

        if not output_file.exists():
            raise RuntimeError("ffmpeg transcoding produced no output file")

        # Read output and encode to base64
        wav_data = output_file.read_bytes()
        return base64.b64encode(wav_data).decode("utf-8")
