"""Shared helpers for the Python-engine smoke tests (real Chromium and PTY clients)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Locator, Page, expect

REPO = Path(__file__).resolve().parents[2]


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def root_conversation(page: Page) -> Locator:
    return page.locator("main > .conversation")


def approval_dialog(page: Page) -> Locator:
    return page.locator("dialog.approval-dialog")


async def connected(page: Page) -> None:
    await expect(page.locator(".topbar .native-connection")).to_have_text("Connected")


async def ready(page: Page, reply: str = "") -> None:
    conversation = root_conversation(page)
    if reply:
        await expect(conversation.locator(".entry.assistant").get_by_text(reply, exact=True)).to_be_visible()
    await expect(page.locator(".run-status")).to_have_text("Ready")
    await expect(conversation.locator(".entry.error")).to_have_count(0)


async def new_mobile_session(page: Page) -> None:
    drawer = page.locator(".sidebar")
    selected = drawer.locator(".session-row.selected")
    previous = await selected.get_attribute("data-session-id") or ""
    require(previous, "No session was selected before creating a new one")
    await page.get_by_role("button", name="Toggle sessions", exact=True).tap()
    await expect(drawer).to_be_visible()
    async with page.expect_response(
        lambda response: response.request.method == "POST" and urlsplit(response.url).path == "/api/v1/sessions",
        timeout=8000,
    ) as creating:
        await drawer.locator(".sidebar-create").get_by_role("button", name="New session", exact=True).tap()
    response = await creating.value
    require(response.ok, "The engine refused to create a session")
    created = (await response.json())["id"]
    require(created != previous, "The engine did not create a new session")
    await expect(drawer).to_be_hidden()
    await expect(selected).to_have_attribute("data-session-id", created)
    await expect(page.locator("#composer")).to_be_focused()
    await connected(page)
    await expect(root_conversation(page)).to_have_count(1)
    await expect(root_conversation(page).locator(".entry")).to_have_count(0)
