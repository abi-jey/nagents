"""Atomic private Live settings, cross-owner revisions and cancellation-safe admission."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from threading import Event as ThreadEvent
from typing import TYPE_CHECKING
from typing import TypeVar

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from nagents.web.live import capabilities
from nagents.web.live_settings import DEFAULT_BASE_URL
from nagents.web.live_settings import LiveSettings
from nagents.web.live_settings import LiveSettingsInput
from nagents.web.live_settings import LiveValues
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

T = TypeVar("T")
SECRET = "sk-private-live-first"
REPLACEMENT = "sk-private-live-replacement"


def values(**changes: object) -> LiveValues:
    return LiveValues.model_validate({**LiveValues.defaults().model_dump(), "enabled": True, **changes})


async def request(store: LiveSettings, *, key: str = "", clear: bool = False, **changes: object) -> LiveSettingsInput:
    connection = await store.connection()
    return LiveSettingsInput(
        revision=connection.revision, values=values(**changes), api_key=SecretStr(key), clear_api_key=clear
    )


def store(path: Path, active: Callable[[], str] = lambda: "") -> LiveSettings:
    return LiveSettings(path / "sessions.db", demo=False, active=active)


async def sql(path: Path, statement: str, parameters: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    def execute() -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(path)) as db, db, closing(db.execute(statement, parameters)) as cursor:
            return cursor.fetchall()

    return await asyncio.to_thread(execute)


def test_key_values_and_revisions_persist_separately_and_never_serialize(tmp_path: Path) -> None:
    async def check() -> None:
        first = store(tmp_path)
        await first.load()
        initial = await first.snapshot()
        assert initial["values"] == LiveValues.defaults().model_dump() and not initial["key_configured"]
        body = await request(first, key=SECRET)
        assert SECRET not in repr(body) and "api_key" not in body.model_dump()
        saved = await first.change(body)
        current = await first.connection()
        assert current.api_key.get_secret_value() == SECRET
        assert saved["key_configured"] and saved["revision"] != initial["revision"]
        assert SECRET not in repr(current) and SECRET not in str(saved) and SECRET not in str(capabilities(current))
        public = await sql(first.db_path, "SELECT values_json, revision FROM ngn_web_live_settings")
        private = await sql(first.db_path, "SELECT provider, endpoint, secret FROM ngn_web_live_key")
        assert SECRET not in str(public) and private == [("openai", DEFAULT_BASE_URL, SECRET)]
        await first.shutdown()
        second = store(tmp_path)
        await second.load()
        assert await second.snapshot() == saved
        assert (await second.connection()).api_key.get_secret_value() == SECRET
        assert not second._tasks

    asyncio.run(check())


def test_keep_replace_and_clear_key_without_environment(tmp_path: Path) -> None:
    async def check() -> None:
        settings = store(tmp_path)
        await settings.load()
        await settings.change(await request(settings, key=SECRET))
        for key, clear, expected in (
            ("", False, SECRET),
            (REPLACEMENT, False, REPLACEMENT),
            ("", True, ""),
            ("", False, ""),
        ):
            before = (await settings.connection()).revision
            result = await settings.change(await request(settings, key=key, clear=clear, voice="cedar"))
            connection = await settings.connection()
            assert connection.api_key.get_secret_value() == expected
            assert result["key_configured"] is bool(expected) and connection.revision != before
            assert SECRET not in str(result) and REPLACEMENT not in str(result)
        assert not (await settings.connection()).key_configured
        assert await sql(settings.db_path, "SELECT * FROM ngn_web_live_key") == []

    asyncio.run(check())


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "openai_compatible"},
        {"base_url": "https://other.example.invalid/v1"},
        {"base_url": "https://api.openai.com/v2"},
        {"base_url": "https://api.openai.com:8443/v1"},
        {"provider": "azure_openai_compatible_v1", "base_url": "https://azure.example.invalid/openai/v1"},
    ],
)
@pytest.mark.parametrize("replacement", ["", REPLACEMENT])
def test_connection_change_invalidates_old_key_unless_replaced(
    tmp_path: Path, changes: dict[str, object], replacement: str
) -> None:
    async def check() -> None:
        settings = store(tmp_path)
        await settings.load()
        await settings.change(await request(settings, key=SECRET))
        body = await request(settings, key=replacement)
        result = await settings.change(body.model_copy(update={"values": values(**changes)}))
        connection = await settings.connection()
        assert connection.api_key.get_secret_value() == replacement
        assert result["key_configured"] is bool(replacement)
        assert capabilities(connection)["available"] is bool(replacement)
        assert SECRET not in str(result)
        assert SECRET not in str(await sql(settings.db_path, "SELECT * FROM ngn_web_live_key"))

    asyncio.run(check())


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://API.OPENAI.COM:443/v1/"])
def test_canonical_endpoint_spelling_retains_same_connection_key(tmp_path: Path, url: str) -> None:
    async def check() -> None:
        settings = store(tmp_path)
        await settings.load()
        await settings.change(await request(settings, key=SECRET))
        await settings.change(await request(settings, base_url=url, model="new-voice-model"))
        assert (await settings.connection()).api_key.get_secret_value() == SECRET
        assert (
            values(provider="azure_openai_compatible_v1", base_url="https://azure.invalid/openai").connection()
            == values(provider="azure_openai_compatible_v1", base_url="https://azure.invalid/openai/v1/").connection()
        )

    asyncio.run(check())


def test_missing_key_or_azure_endpoint_can_be_saved_and_has_actionable_reason(tmp_path: Path) -> None:
    async def check() -> None:
        settings = store(tmp_path)
        await settings.load()
        await settings.change(await request(settings))
        info = capabilities(await settings.connection())
        assert not info["available"] and "Connection settings" in str(info["reason"])
        await settings.change(await request(settings, provider="azure_openai_compatible_v1", key=SECRET))
        info = capabilities(await settings.connection())
        assert not info["available"] and "base URL" in str(info["reason"])

    asyncio.run(check())


def test_secrets_cannot_be_copied_into_public_settings_even_when_cleared(tmp_path: Path) -> None:
    async def check() -> None:
        settings = store(tmp_path)
        await settings.load()
        for changes in ({"model": SECRET}, {"base_url": f"https://voice.invalid/{SECRET}"}):
            with pytest.raises(HTTPException) as rejected:
                body = await request(settings, key=SECRET)
                await settings.change(body.model_copy(update={"values": values(**changes)}))
            assert rejected.value.status_code == 422 and SECRET not in str(rejected.value)
        await settings.change(await request(settings, key=SECRET))
        with pytest.raises(HTTPException) as rejected:
            await settings.change(await request(settings, clear=True, backend_model=SECRET))
        assert rejected.value.status_code == 422 and SECRET not in str(rejected.value)
        assert SECRET not in str(await settings.snapshot())

    asyncio.run(check())


def test_independent_owners_have_atomic_compare_and_swap_and_fresh_reads(tmp_path: Path) -> None:
    async def check() -> None:
        first, second = store(tmp_path), store(tmp_path)
        await asyncio.gather(first.load(), second.load())
        assert await first.snapshot() == await second.snapshot()
        one, two = await request(first, key=SECRET), await request(second, key=REPLACEMENT, voice="cedar")
        outcomes = await asyncio.gather(first.change(one), second.change(two), return_exceptions=True)
        assert sum(isinstance(result, dict) for result in outcomes) == 1
        rejected = next(result for result in outcomes if isinstance(result, HTTPException))
        assert rejected.status_code == 409
        assert await first.snapshot() == await second.snapshot()
        for owner in (first, second):
            with pytest.raises(HTTPException) as stale:
                async with owner.admit(one.revision):
                    pytest.fail("Stale revision must not create a call")
            assert stale.value.status_code == 409
        winner = await first.connection()
        assert (winner.values.voice, winner.api_key.get_secret_value()) in (("marin", SECRET), ("cedar", REPLACEMENT))

    asyncio.run(check())


def test_admission_pins_one_committed_connection_for_inherited_supervisor(tmp_path: Path) -> None:
    async def check() -> None:
        first, other = store(tmp_path), store(tmp_path)
        await first.load()
        await other.load()
        await first.change(await request(first, key=SECRET))
        connection = await first.connection()
        async with first.admit(connection.revision):
            # A separate process can write later, but cannot reroute an already admitted call.
            await other.change(await request(other, key=REPLACEMENT, base_url="https://other.invalid/v1"))
            assert (await first.connection()).api_key.get_secret_value() == REPLACEMENT

            async def supervise() -> tuple[str, str]:
                captured = first.admitted()
                return captured.values.base_url, captured.api_key.get_secret_value()

            assert await asyncio.create_task(supervise()) == ("", SECRET)
        with pytest.raises(HTTPException):
            first.admitted()

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["connecting", "connected", "closing"])
def test_active_reservation_prevents_saves_in_every_phase(tmp_path: Path, phase: str) -> None:
    async def check() -> None:
        state = [""]
        settings = store(tmp_path, lambda: state[0])
        await settings.load()
        body = await request(settings, key=SECRET)
        state[0] = phase
        with pytest.raises(HTTPException) as error:
            await settings.change(body)
        assert error.value.status_code == 409 and not (await settings.connection()).key_configured

    asyncio.run(check())


def test_save_and_admission_reserve_before_first_database_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        settings = store(tmp_path)
        await settings.load()
        await settings.change(await request(settings, key=SECRET))
        body = await request(settings, key=REPLACEMENT)
        entered, release = asyncio.Event(), asyncio.Event()
        transaction = settings._transaction

        async def gated(operation: Callable[[sqlite3.Connection], T]) -> T:
            entered.set()
            await release.wait()
            return await transaction(operation)

        monkeypatch.setattr(settings, "_transaction", gated)
        saving = asyncio.create_task(settings.change(body))
        await entered.wait()
        try:
            with pytest.raises(HTTPException) as blocked:
                async with settings.admit(body.revision):
                    pytest.fail("A call cannot capture settings during a save")
            assert blocked.value.status_code == 409
        finally:
            release.set()
        await saving
        new_revision = (await settings.connection()).revision
        entered.clear()
        release.clear()

        async def admitting() -> None:
            async with settings.admit(new_revision):
                assert settings.admitted().api_key.get_secret_value() == REPLACEMENT

        creation = asyncio.create_task(admitting())
        await entered.wait()
        try:
            with pytest.raises(HTTPException) as blocked:
                await settings.change(body)
            assert blocked.value.status_code == 409
        finally:
            release.set()
        await creation

    asyncio.run(check())


def test_failed_private_write_rolls_back_public_settings_revision_and_key(tmp_path: Path) -> None:
    async def check() -> None:
        settings = store(tmp_path)
        await settings.load()
        await settings.change(await request(settings, key=SECRET))
        before = await settings.snapshot()
        await sql(
            settings.db_path,
            f"CREATE TRIGGER reject_key BEFORE INSERT ON ngn_web_live_key BEGIN SELECT RAISE(ABORT, '{REPLACEMENT}'); END",
        )
        with pytest.raises(HTTPException) as error:
            await settings.change(await request(settings, key=REPLACEMENT, voice="cedar"))
        assert error.value.status_code == 500 and REPLACEMENT not in str(error.value)
        assert await settings.snapshot() == before and not settings._busy
        reloaded = store(tmp_path)
        await reloaded.load()
        assert await reloaded.snapshot() == before
        assert (await reloaded.connection()).api_key.get_secret_value() == SECRET

    asyncio.run(check())


@pytest.mark.parametrize("damage", ["values", "key-binding", "key-value", "version"])
def test_failed_reload_never_uses_a_stale_cached_connection(tmp_path: Path, damage: str) -> None:
    async def check() -> None:
        settings = store(tmp_path)
        await settings.load()
        await settings.change(await request(settings, key=SECRET))
        revision = (await settings.connection()).revision
        statements = {
            "values": ("UPDATE ngn_web_live_settings SET values_json = ?", (SECRET,)),
            "key-binding": ("UPDATE ngn_web_live_key SET endpoint = ?", (f"https://{SECRET}.invalid/v1",)),
            "key-value": ("UPDATE ngn_web_live_key SET secret = ?", (SECRET + "\n",)),
            "version": ("UPDATE ngn_web_live_settings SET version = ?", (99,)),
        }
        statement, parameters = statements[damage]
        await sql(settings.db_path, statement, parameters)
        for operation in (settings.connection, settings.load):
            with pytest.raises(HTTPException) as error:
                await operation()
            assert error.value.status_code == 503 and SECRET not in str(error.value)
        with pytest.raises(HTTPException) as error:
            async with settings.admit(revision):
                pytest.fail("Unreadable state cannot admit a call")
        assert error.value.status_code == 503 and not settings._busy and not settings._tasks

    asyncio.run(check())


@pytest.mark.parametrize("cancel_shutdown", [False, True])
def test_cancelled_save_finishes_atomic_commit_and_shutdown_joins_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_shutdown: bool
) -> None:
    async def check() -> None:
        settings = store(tmp_path)
        await settings.load()
        await settings.change(await request(settings, key=SECRET))
        before = (await settings.connection()).revision
        body = await request(settings, key=REPLACEMENT, voice="cedar")
        reached, release = asyncio.Event(), ThreadEvent()
        loop = asyncio.get_running_loop()
        transaction = settings._transaction

        async def gated(operation: Callable[[sqlite3.Connection], T]) -> T:
            def paused(db: sqlite3.Connection) -> T:
                result = operation(db)
                loop.call_soon_threadsafe(reached.set)
                assert release.wait(HANG_GUARD)
                return result

            return await transaction(paused)

        monkeypatch.setattr(settings, "_transaction", gated)
        saving = asyncio.create_task(settings.change(body))
        await reached.wait()
        saving.cancel()
        await asyncio.sleep(0)
        saving.cancel()
        closing = asyncio.create_task(settings.shutdown())
        try:
            await asyncio.sleep(0)
            if cancel_shutdown:
                closing.cancel()
                await asyncio.sleep(0)
                closing.cancel()
            assert settings._busy and not saving.done() and not closing.done()
        finally:
            release.set()
        result = await asyncio.wait_for(asyncio.gather(saving, closing, return_exceptions=True), HANG_GUARD)
        assert isinstance(result[0], asyncio.CancelledError)
        assert isinstance(result[1], asyncio.CancelledError) if cancel_shutdown else result[1] is None
        assert not settings._busy and not settings._tasks
        reloaded = store(tmp_path)
        await reloaded.load()
        saved = await reloaded.connection()
        assert saved.revision != before and saved.values.voice == "cedar"
        assert saved.api_key.get_secret_value() == REPLACEMENT

    asyncio.run(check())
