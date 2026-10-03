"""The TUI editor writes the same secret-free HOME configuration as web."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual.widgets import Button
from textual.widgets import Input
from textual.widgets import Static

from nagents.harness.config import HarnessConfig
from nagents.harness.providers import ProviderProfile
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
            assert app.screen.query_one("#provider-request-timeout", Input).value == "120.0"
            app.screen.query_one("#provider-request-timeout", Input).value = "240.5"
            await pilot.click("#provider-save")
            await idle(app, pilot)
            store = ScopedProviderRegistryStore(tmp_path)
            registry = store.load()
            assert registry.active == "home"
            assert registry.providers["home"].key_env == "TUI_TEST_KEY"
            assert registry.providers["home"].request_timeout == 240.5
            assert app.harness.config.provider_id == "home"
            assert "TUI_TEST_KEY" in store.workspace_store.path.read_text()
            assert not store.global_store.path.exists()
            assert not (tmp_path / "user-data/ngn/auth/login.json").exists()

    asyncio.run(check())


def test_tui_preserves_saved_request_timeout_and_rejects_invalid_edits(tmp_path: Path) -> None:
    async def check() -> None:
        app = NagentsApp(Harness(HarnessConfig(workspace=tmp_path, demo=True)))
        results: list[tuple[str, ProviderProfile] | None] = []
        profile = ProviderProfile(kind="openai", auth="api-key", request_timeout=375)
        async with app.run_test(size=(100, 45)) as pilot:
            await idle(app, pilot)
            editor = ProviderEditor("existing", profile)
            await app.push_screen(editor, results.append)
            timeout = editor.query_one("#provider-request-timeout", Input)
            assert float(timeout.value) == 375
            for value in ["0", "-2", "nan", "inf", "1e999", "", "seconds"]:
                timeout.value = value
                editor.query_one("#provider-save", Button).press()
                await pilot.pause()
                assert app.screen is editor and not results
                assert "positive finite" in str(editor.query_one("#provider-error", Static).render())
            timeout.value = "375"
            editor.query_one("#provider-env", Input).value = "UPDATED_ENV"
            editor.query_one("#provider-save", Button).press()
            await pilot.pause()
            assert len(results) == 1 and results[0] is not None, str(
                editor.query_one("#provider-error", Static).render()
            )
            assert results[0][1].request_timeout == 375
            assert results[0][1].api_key_env == "UPDATED_ENV"

    asyncio.run(check())
