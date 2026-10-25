"""Scoped child diagnostics preserve chat/voice outcomes and root progress."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.events import ErrorEvent
from nagents.events import ToolCallProgressEvent
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.live.delegation import ClientDelegationRequest
from nagents.web._async import finish_on_cancel
from nagents.web.live_bridge import MainAgentBridge
from nagents.web.service import WebState
from tests.harness.test_harness import ScriptedProvider
from tests.support.child_failures import ChildFailureScenario
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import LiveStream
from tests.support.web import client_app
from tests.support.web import submit_and_wait

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.service import Run
    from tests.support.providers import FakeProvider


def fixture_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HarnessConfig:
    """A named configured connection admits chat while transport stays scripted."""
    monkeypatch.setenv("NGN_CHILD_FAILURE_TEST_KEY", "test-only-scripted-key")
    selected = connection(name="fixture", auth="api-key", api_key_env="NGN_CHILD_FAILURE_TEST_KEY")
    store = ScopedProviderRegistryStore(tmp_path).workspace_store
    store.save(ProviderRegistry(active="fixture", providers=selected), expected=store.load().revision)
    return HarnessConfig(tmp_path, data_dir=tmp_path / "state", provider="fixture", providers=selected)


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def terminal_reports(reports: list[dict[str, object]]) -> list[dict[str, object]]:
    return [report for report in reports if report.get("status") in TERMINAL_STATUSES]


@asynccontextmanager
async def voice_request(
    state: WebState, session_id: str
) -> AsyncIterator[tuple[list[dict[str, object]], asyncio.Task[str]]]:
    reports: list[dict[str, object]] = []
    bridge = MainAgentBridge(state, session_id, report=reports.append)

    async def append(kind: str, content: str, identifier: str) -> str:
        return f"session.{kind}.append"

    bridge.attach(append)
    speech = asyncio.create_task(bridge.handle_request(ClientDelegationRequest("root", "ROOT")))
    try:
        yield reports, speech
    finally:
        speech.cancel()
        try:
            await asyncio.gather(speech, return_exceptions=True)
        finally:
            await bridge.close()


async def voice_terminal(reports: list[dict[str, object]]) -> dict[str, object]:
    async with asyncio.timeout(HANG_GUARD):
        while not (terminal := terminal_reports(reports)):
            await asyncio.sleep(0.001)
    return terminal[-1]


async def voice_progress(
    speech: asyncio.Task[str], reports: list[dict[str, object]], *milestones: asyncio.Event
) -> None:
    async with asyncio.timeout(HANG_GUARD):
        while True:
            if speech.done():
                assert not speech.cancelled(), "Voice admission was cancelled before child progress"
                error = speech.exception()
                if error is not None:
                    pytest.fail(f"Voice admission failed: exception={type(error).__name__[:80]}")
            terminal = terminal_reports(reports)
            if terminal:
                statuses = [report["status"] for report in terminal]
                reached = sum(milestone.is_set() for milestone in milestones)
                pytest.fail(
                    f"Voice run ended before child progress: statuses={statuses} "
                    f"report_count={len(reports)} milestones_reached={reached}"
                )
            if all(milestone.is_set() for milestone in milestones):
                return
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True])
@pytest.mark.parametrize("wrong_parent", [False, True])
@pytest.mark.parametrize(
    "_sqlite_probe",
    range(20) if os.environ.get("NGN_SQLITE_TRACE") == "1" else [0],
)
async def test_recovered_root_keeps_child_failures_and_safe_scope_in_chat_and_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, voice: bool, wrong_parent: bool, _sqlite_probe: int
) -> None:
    scenario = ChildFailureScenario(monkeypatch, wrong_parent=wrong_parent)
    records: list[dict[str, object]] = []
    protected_drafts: list[str] = []
    send = WebState.send

    async def capture(state: WebState, run: Run, record: dict[str, object]) -> None:
        warning = record.get("code") == "TASK_DELIVERY_SKIPPED"
        drafts = {key: item for key, item in run.drafts.items() if item.get("generation_id") == "root-progress"}
        if warning:
            assert len(drafts) == 1
        await send(state, run, record)
        records.append(dict(record))
        if warning:
            assert all(run.drafts.get(key) is item for key, item in drafts.items())
            protected_drafts.extend(drafts)

    monkeypatch.setattr(WebState, "send", capture)
    config = fixture_config(tmp_path, monkeypatch)
    config.max_tool_rounds = 6
    async with client_app(tmp_path, config=config, controlled=False) as (app, _, headers, harnesses):
        harness = harnesses[0]
        state = cast("WebState", app.state.web)
        expected = "failed" if wrong_parent else "completed"
        async with asyncio.timeout(HANG_GUARD):
            if voice:
                async with voice_request(state, harness.session_id) as (reports, speech):
                    await speech
                    terminal = await voice_terminal(reports)
                    status = terminal["status"]
                    assert status == expected, {"status": status, "expected": expected, "report_count": len(reports)}
                    await state.channels.join_claim_cleanup(harness.session_id)
                    assert "PRIVATE_UPSTREAM_DETAIL" not in str(terminal)
                    if not wrong_parent:
                        assert terminal["result_text"] == "ROOT_RECOVERED"
            else:
                wire = await submit_and_wait(app, headers, harness.session_id, "ROOT")
                assert next(record for record in wire if record["event"] == "run_finished")["status"] == expected
        scenario.assert_recovered()
        completions = [record for record in records if record["event"] == "task_completed"]
        assert sorted((record["depth"], record["status"]) for record in completions) == [
            (1, "completed"),
            (1, "failed"),
            (2, "cancelled"),
        ]
        descendant = next(record for record in completions if record["depth"] == 2)
        assert descendant["result"] == "" and descendant["error"]
        error = next(record for record in records if record["event"] == "error")
        if wrong_parent:
            assert error["recoverable"] is False and "task_id" not in error and "session_id" not in error
            assert "code" not in error
            assert not protected_drafts
        else:
            assert error["recoverable"] is True and error["code"] == "TASK_DELIVERY_SKIPPED"
            assert "parent stopped" in str(error["message"]) and "Provider" not in str(error["message"])
            for key in (
                "session_id",
                "title",
                "parent_session_id",
                "root_session_id",
                "depth",
                "activation",
                "followup",
            ):
                assert error[key] == descendant[key]
            assert "extra" not in error and protected_drafts
        assert not any(
            record.get("source_session_id") == descendant["session_id"]
            for record in records
            if record["event"] == "task_notification"
        )
        assert state.active is None
    assert all(provider.closed for provider in scenario.providers)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["TASK_DELIVERY_SKIPPED", "CODEX_HTTP_500"])
async def test_upstream_error_code_cannot_spoof_task_scope_or_hide_a_root_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    config = fixture_config(tmp_path, monkeypatch)
    async with client_app(tmp_path, config=config) as (app, _, headers, harnesses):
        harness = harnesses[0]
        harness.run = Harness.run.__get__(harness)  # type: ignore[method-assign]
        harness.agent.provider = ScriptedProvider(
            [
                [
                    ToolCallProgressEvent(generation_id="root", index=0, name="read_file", arguments_text="{"),
                    ErrorEvent(
                        code=code,
                        message="PRIVATE_UPSTREAM_DETAIL",
                        extra={
                            "task_id": "legacy-forged-child",
                            "session_id": "s999",
                            "activation": 123,
                            "password": "PRIVATE_UPSTREAM_DETAIL",
                        },
                    ),
                ]
            ]
        )
        async with asyncio.timeout(HANG_GUARD):
            records = await submit_and_wait(app, headers, harness.session_id, "ROOT")
        assert "PRIVATE_UPSTREAM_DETAIL" not in json.dumps(records)
        error = next(record for record in records if record["event"] == "error")
        assert error["recoverable"] is False
        assert not {"extra", "task_id", "session_id", "activation", "password", "code"} & error.keys()
        assert next(record for record in records if record["event"] == "run_finished")["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True])
async def test_stop_still_cancels_chat_and_voice_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, voice: bool
) -> None:
    scenario = ChildFailureScenario(monkeypatch, hold_child=True)
    config = fixture_config(tmp_path, monkeypatch)
    config.max_tool_rounds = 6
    async with client_app(tmp_path, config=config, controlled=False) as (app, client, headers, harnesses):
        harness = harnesses[0]
        state = cast("WebState", app.state.web)
        async with asyncio.timeout(HANG_GUARD), AsyncExitStack() as cleanup:
            if voice:
                reports, speech = await cleanup.enter_async_context(voice_request(state, harness.session_id))
                await voice_progress(speech, reports, scenario.grandchild_started, scenario.root_preview)
            else:
                stream = LiveStream(app, headers, harness.session_id, "ROOT")
                await scenario.grandchild_started.wait()
                await scenario.root_preview.wait()
            assert state.active is not None
            response = await client.post("/api/cancel", headers=headers, json={"run_id": state.active.id})
            assert response.status_code == 200, response.text
            if voice:
                await speech
                terminal = await voice_terminal(reports)
                status = terminal["status"]
                assert status == "cancelled", {"status": status, "report_count": len(reports)}
                assert "result_text" not in terminal
            else:
                finished = await stream.event("run_finished")
                records = [finished]
                assert finished["status"] == "cancelled", {
                    "event_count": len(records),
                    "provider_requests": [len(provider.requests) for provider in scenario.providers],
                    "task_statuses": [info.status for info in harness.tasks.list()],
                }
        assert {info.status for info in harness.tasks.list()} == {"cancelled"}
        assert len(scenario.providers) == 3 and all(provider.closed for provider in scenario.providers[1:])
        assert "DESCENDANT_PRIVATE" not in str(scenario.providers[0].requests)
        assert state.active is None


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True])
async def test_stop_reaches_providers_before_pending_task_evidence_commit_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, voice: bool
) -> None:
    scenario = ChildFailureScenario(monkeypatch, hold_child=True)
    root_cancelled, descendant_cancelled = asyncio.Event(), asyncio.Event()
    script = scenario.script

    async def observed(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        try:
            async for event in script(provider, messages):
                yield event
        except asyncio.CancelledError:
            if provider.index == 0:
                root_cancelled.set()
            elif provider.index == 2:
                descendant_cancelled.set()
            raise

    monkeypatch.setattr(scenario, "script", observed)
    config = fixture_config(tmp_path, monkeypatch)
    config.max_tool_rounds = 6
    async with client_app(tmp_path, config=config, controlled=False) as (app, client, headers, harnesses):
        state = cast("WebState", app.state.web)
        harness = harnesses[0]
        entered, release, committed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        save = state.tasks.store.save
        held = False

        async def delayed_save(root: str, data: dict[str, object], storage_id: str) -> None:
            nonlocal held
            if held:
                await save(root, data, storage_id)
                return
            held = True

            async def committing() -> None:
                entered.set()
                await release.wait()
                await save(root, data, storage_id)
                committed.set()

            # Like the actual SQLite owner, finish an accepted operation even
            # when the event consumer waiting for it receives cancellation.
            await finish_on_cancel(committing())

        monkeypatch.setattr(state.tasks.store, "save", delayed_save)
        async with asyncio.timeout(HANG_GUARD), AsyncExitStack() as cleanup:
            if voice:
                reports, speech = await cleanup.enter_async_context(voice_request(state, harness.session_id))
                await voice_progress(speech, reports, scenario.grandchild_started, scenario.root_preview)
            else:
                stream = LiveStream(app, headers, harness.session_id, "ROOT")
                await scenario.grandchild_started.wait()
                await scenario.root_preview.wait()
            await entered.wait()
            run = state.active
            assert run is not None
            stopping = asyncio.create_task(client.post("/api/cancel", headers=headers, json={"run_id": run.id}))
            try:
                # Provider code must observe cancellation while the accepted
                # write is still held. This checks behavior, not Task flags or
                # a particular scheduling/latency budget.
                await root_cancelled.wait()
                await descendant_cancelled.wait()
                assert not release.is_set() and not committed.is_set() and not stopping.done()
                assert [len(provider.requests) for provider in scenario.providers] == [2, 1, 1]
            finally:
                release.set()
                response = await stopping
            assert response.status_code == 200, response.text
            assert committed.is_set(), "Stop must join the accepted evidence write"
            if voice:
                await speech
                assert (await voice_terminal(reports))["status"] == "cancelled"
            else:
                assert (await stream.event("run_finished"))["status"] == "cancelled"
            assert [len(provider.requests) for provider in scenario.providers] == [2, 1, 1]
            assert {info.status for info in harness.tasks.list()} == {"cancelled"}
            assert all(provider.closed for provider in scenario.providers[1:])
            assert state.active is None and harness._worker is None
