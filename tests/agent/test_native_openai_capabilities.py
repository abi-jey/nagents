"""Native OpenAI capability gates must retain API-key/subscription boundaries."""

from pathlib import Path

import pytest

from nagents import Agent
from nagents import OpenAIProvider
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.media import get_media_capabilities
from nagents.provider import CodexCredentials
from nagents.types import AudioContent


@pytest.mark.asyncio
async def test_native_audio_model_passes_audio_through_agent(tmp_path: Path) -> None:
    provider = Provider(ProviderType.OPENAI, api_key="fixture-key", model="gpt-audio-1.5")
    agent = Agent(provider, SessionManager(tmp_path / "audio.db"), compaction_strategy=None)
    audio = AudioContent(base64_data="UklGRg==", format="wav")
    try:
        assert await agent._process_multimodal_content([audio]) == [audio]
    finally:
        await agent.close()


@pytest.mark.parametrize("kind", [ProviderType.OPENAI, ProviderType.OPENAI_COMPATIBLE])
@pytest.mark.asyncio
async def test_api_key_batch_and_realtime_are_supported(tmp_path: Path, kind: ProviderType) -> None:
    provider = Provider(kind, api_key="fixture-key", model="gpt-realtime-2.1")
    agent = Agent(provider, SessionManager(tmp_path / "agent.db"), batch=True, compaction_strategy=None)
    try:
        assert agent._batch_client is not None
        assert agent._batch_client.provider_type is kind
        assert agent._batch_client._get_headers()["Authorization"] == "Bearer fixture-key"
        session = agent.realtime_session()
        assert session._build_session()["model"] == "gpt-realtime-2.1"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_native_api_key_openai_provider_supports_batch(tmp_path: Path) -> None:
    provider = OpenAIProvider(api_key="fixture-key", model="gpt-4o")
    agent = Agent(provider, SessionManager(tmp_path / "agent.db"), batch=True, compaction_strategy=None)
    try:
        assert agent._batch_client is not None
        assert agent._batch_client.provider_type is ProviderType.OPENAI
        assert not provider.uses_chatgpt_auth
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_subscription_cannot_create_batch_or_realtime(tmp_path: Path) -> None:
    async def credentials() -> CodexCredentials:
        raise AssertionError("Capability checks must not read subscription credentials")

    provider = OpenAIProvider(credentials=credentials, model="gpt-4o")
    agent = Agent(provider, SessionManager(tmp_path / "agent.db"), compaction_strategy=None)
    try:
        with pytest.raises(ValueError, match="Batch mode requires API-key authentication"):
            agent._init_batch_client()
        assert agent._batch_client is None
        with pytest.raises(ValueError, match="Realtime requires API-key authentication"):
            agent.realtime_session()
        with pytest.raises(ValueError, match="Batch mode requires API-key authentication"):
            Agent(provider, SessionManager(tmp_path / "batch.db"), batch=True, compaction_strategy=None)
    finally:
        await agent.close()


@pytest.mark.parametrize(
    "model",
    ["gpt-4o", "unknown", "gpt-4o-audio-preview", "gpt-4o-mini-audio-preview-2024-12-17", "gpt-audio-1.5"],
)
def test_native_media_matches_compatible_capabilities(model: str) -> None:
    native = get_media_capabilities(ProviderType.OPENAI.value, model)
    assert native == get_media_capabilities(ProviderType.OPENAI_COMPATIBLE.value, model)
    assert native.supports_image("png")
    assert not native.supports_document("pdf")
    assert native.supports_audio("wav") == ("audio" in model)
    assert native.supports_audio("mp3") == ("audio" in model)
    assert not native.supports_audio("ogg")
