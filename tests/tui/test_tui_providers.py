"""The TUI editor writes the same secret-free HOME configuration as web."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual.widgets import Input

from nagents.harness.config import HarnessConfig
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.runtime import Harness
from nagents.tui.app import NagentsApp
from nagents.tui.providers import ProviderEditor
from nagents.tui.screens import ChoiceModal
from tests.support.tui import idle
from tests.support.tui import send

if TYPE_CHECKING:
    from pathlib import Path


def test_tui_adds_named_connection_with_only_env_reference(tmp_path: Path) -> None:
    async def check() -> None:
        app = NagentsApp(Harness(HarnessConfig(workspace=tmp_path, demo=True)))
        async with app.run_test(size=(100, 40)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "/provider")
            assert isinstance(app.screen, ChoiceModal)
            await pilot.press("enter")
            assert isinstance(app.screen, ProviderEditor)
            app.screen.query_one("#provider-name", Input).value = "home"
            app.screen.query_one("#provider-env", Input).value = "${TUI_TEST_KEY}"
            await pilot.click("#provider-save")
            await idle(app, pilot)
            store = ScopedProviderRegistryStore(tmp_path)
            registry = store.load()
            assert registry.active == "home"
            assert registry.providers["home"].key_env == "TUI_TEST_KEY"
            assert app.harness.config.provider_id == "home"
            assert "TUI_TEST_KEY" in store.workspace_store.path.read_text()
            assert not store.global_store.path.exists()
            assert not (tmp_path / "user-data/ngn/auth/login.json").exists()

    asyncio.run(check())
