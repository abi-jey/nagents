"""Tests for the Realtime speech-to-speech integration (no network required)."""

import asyncio
import wave
from pathlib import Path

from nagents.adapters.openai import format_realtime_tools
from nagents.events import RealtimeRateLimitsEvent
from nagents.events import ResponseCancelledEvent
from nagents.events import ResponseCreatedEvent
from nagents.realtime import AudioFormat
from nagents.realtime import BytesAudioInput
from nagents.realtime import BytesAudioOutput
from nagents.realtime import RealtimeConfig
from nagents.realtime import RealtimeSession
from nagents.realtime import WAVFileAudioInput
from nagents.realtime import WAVFileAudioOutput
from nagents.types import ToolDefinition


def _weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Weather in {city}: sunny"


def test_format_realtime_tools() -> None:
    tool = ToolDefinition(
        name="get_weather",
        description="Get the weather.",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
    )
    result = format_realtime_tools([tool])
    assert result == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get the weather.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]


def test_realtime_config_defaults() -> None:
    config = RealtimeConfig()
    assert config.model is None
    assert config.voice == "marin"
    assert config.output_modalities == ["audio"]
    assert config.input_audio_rate == 24000
    assert config.turn_detection is True


def test_model_resolution() -> None:
    # No model anywhere -> default realtime model.
    session = RealtimeSession(api_key="sk-test")
    assert session._build_session()["model"] == "gpt-realtime-2.1"

    # Config model wins.
    session = RealtimeSession(api_key="sk-test", config=RealtimeConfig(model="gpt-realtime-2.1-mini"))
    assert session._build_session()["model"] == "gpt-realtime-2.1-mini"

    # Explicit model param wins over default when config has none.
    session = RealtimeSession(api_key="sk-test", model="gpt-realtime-mini")
    assert session._build_session()["model"] == "gpt-realtime-mini"


def test_build_session_uses_adapter_formats() -> None:
    session = RealtimeSession(api_key="sk-test")
    assert session._build_session()["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}

    session = RealtimeSession(
        api_key="sk-test",
        audio_in=BytesAudioInput(b"", audio_format=AudioFormat(sample_rate=8000)),
        audio_out=BytesAudioOutput(AudioFormat(sample_rate=16000)),
    )
    audio = session._build_session()["audio"]
    assert audio["input"]["format"] == {"type": "audio/pcm", "rate": 8000}
    assert audio["output"]["format"] == {"type": "audio/pcm", "rate": 16000}


def test_build_session_includes_tools_and_config() -> None:
    session = RealtimeSession(
        api_key="sk-test",
        tools=[_weather],
        config=RealtimeConfig(
            instructions="Be brief.",
            reasoning_effort="low",
            max_output_tokens=1000,
            turn_detection=False,
            input_transcription_model="gpt-4o-mini-transcribe",
        ),
    )
    built = session._build_session()
    assert built["model"] == "gpt-realtime-2.1"
    assert built["instructions"] == "Be brief."
    assert built["reasoning"] == {"effort": "low"}
    assert built["max_output_tokens"] == 1000
    assert built["audio"]["input"]["turn_detection"] is None
    assert built["audio"]["input"]["transcription"] == {"model": "gpt-4o-mini-transcribe"}
    assert built["tools"] == [
        {
            "type": "function",
            "name": "_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]


def test_bytes_audio_adapters() -> None:
    async def run() -> None:
        src = BytesAudioInput(b"\x00\x01\x02\x03", chunk_size=2)
        chunks = [chunk async for chunk in src]
        assert chunks == [b"\x00\x01", b"\x02\x03"]

        out = BytesAudioOutput()
        for chunk in chunks:
            await out.write(chunk)
        await out.interrupt()
        await out.close()
        assert out.data == b"\x00\x01\x02\x03"

    asyncio.run(run())


def test_handle_response_lifecycle_events() -> None:
    async def run() -> None:
        session = RealtimeSession(api_key="sk-test")

        events = await session._handle_server_event({"type": "response.created"})
        assert len(events) == 1
        assert isinstance(events[0], ResponseCreatedEvent)

        events = await session._handle_server_event({"type": "response.cancelled"})
        assert len(events) == 1
        assert isinstance(events[0], ResponseCancelledEvent)

        events = await session._handle_server_event(
            {"type": "rate_limits.updated", "rate_limits": [{"name": "requests", "remaining": 5}]}
        )
        assert len(events) == 1
        assert isinstance(events[0], RealtimeRateLimitsEvent)
        assert events[0].rate_limits == [{"name": "requests", "remaining": 5}]

    asyncio.run(run())


def test_wav_file_adapters_roundtrip(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "input.wav"
        pcm = b"\x00\x00" * 100
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(pcm)

        src = WAVFileAudioInput(path)
        assert src.audio_format == AudioFormat(encoding="audio/pcm", sample_rate=24000, channels=1, sample_width=2)
        data = b"".join([chunk async for chunk in src])
        assert data == pcm

        out_path = tmp_path / "output.wav"
        out = WAVFileAudioOutput(out_path)
        await out.write(pcm)
        await out.close()

        with wave.open(str(out_path), "rb") as wf:
            assert wf.getframerate() == 24000
            assert wf.readframes(wf.getnframes()) == pcm

    asyncio.run(run())
