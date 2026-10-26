"""Connection deletion preserves model references, including inactive profiles."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.harness.config import HarnessConfig
from nagents.harness.providers.model_bindings import ModelBinding
from nagents.harness.providers.registry import ProviderProfile
from nagents.harness.providers.registry import ProviderRegistry
from nagents.harness.providers.registry import ScopedProviderRegistryStore
from nagents.harness.runtime import Harness

if TYPE_CHECKING:
    from pathlib import Path

EMPTY_REVISION = "0" * 64


@pytest.mark.parametrize("scope", ["global", "workspace"])
@pytest.mark.parametrize("reference", ["global", "workspace", "assistant", "retired-agent"])
def test_deletion_rejects_saved_model_references_until_cleared(tmp_path: Path, scope: str, reference: str) -> None:
    async def scenario() -> None:
        store = ScopedProviderRegistryStore(tmp_path)
        profile = ProviderProfile(kind="openai", auth="api-key")
        store.global_store.save(
            ProviderRegistry(
                active="active", providers={"active": profile, **({"bound": profile} if scope == "global" else {})}
            ),
            expected=EMPTY_REVISION,
        )
        if scope == "workspace":
            store.workspace_store.save(ProviderRegistry(providers={"bound": profile}), expected=EMPTY_REVISION)
        config = HarnessConfig(
            tmp_path,
            demo=True,
            data_dir=tmp_path / "data",
            provider="active",
            providers=store.load().providers,
            model="default-model",
        )
        harness = Harness(config)
        try:
            binding = ModelBinding("bound", "chosen-model")
            if reference in {"global", "workspace"}:
                store.model_store(reference).save_binding(binding)
            else:
                store.agent_models.save(reference, binding, expected=EMPTY_REVISION)
            before = store.load_scope(scope)
            with pytest.raises(ValueError, match="model bindings"):
                await harness.delete_provider("bound", before.revision, scope)
            assert store.load_scope(scope) == before
            if reference in {"global", "workspace"}:
                assert store.model_binding(reference) == binding
                store.model_store(reference).save_binding(ModelBinding())
            else:
                saved = store.agent_models.load(reference)
                assert saved.binding == binding
                store.agent_models.reset(reference, expected=saved.revision)
            result = await harness.delete_provider("bound", before.revision, scope)
            assert "bound" not in result.providers
            assert store.load().active == "active"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_deletion_rechecks_new_definitions_without_a_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ScopedProviderRegistryStore(tmp_path)
        profile = ProviderProfile(kind="openai", auth="api-key")
        saved = store.global_store.save(
            ProviderRegistry(active="active", providers={"active": profile, "bound": profile}), expected=EMPTY_REVISION
        )
        harness = Harness(
            HarnessConfig(
                tmp_path,
                demo=True,
                trust_project=True,
                data_dir=tmp_path / "data",
                provider="active",
                providers=store.load().providers,
                model="default-model",
            )
        )
        try:
            directory = tmp_path / ".ngn" / "agents"
            directory.mkdir(parents=True)
            (directory / "fresh.md").write_text(
                "---\nname: fresh\ndescription: Newly discovered agent\nmodel: {provider: bound, id: fresh-model}\n"
                "prompt: Help with the task.\n---\n"
            )
            with pytest.raises(ValueError, match="model bindings"):
                await harness.delete_provider("bound", saved.revision, "global")
            assert "fresh" in harness.config.profile_names
            assert store.load_scope("global") == saved
        finally:
            await harness.close()

    asyncio.run(scenario())
