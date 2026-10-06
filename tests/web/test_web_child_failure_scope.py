"""Scoped child diagnostics preserve chat/voice outcomes and root progress."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.events import ErrorEvent
from nagents.events import ToolCallProgressEvent
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.web.live_bridge import MainAgentBridge
from nagents.web.service import WebState
from tests.harness.test_harness import ScriptedProvider
from tests.support.child_failures import ChildFailureScenario
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.web.service import Run


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True])
@pytest.mark.parametrize("wrong_parent", [False, True])
async def test_recovered_root_keeps_child_failures_and_safe_scope_in_chat_and_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, voice: bool, wrong_parent: bool
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
    config = HarnessConfig(tmp_path, data_dir=tmp_path / "state", auth="api-key", max_tool_rounds=6)
    async with client_app(tmp_path, config=config, controlled=False) as (app, client, headers, harnesses):
        harness = harnesses[0]
        state = cast("WebState", app.state.web)
        expected = "failed" if wrong_parent else "completed"
        async with asyncio.timeout(HANG_GUARD):
            if voice:
                reports: list[dict[str, object]] = []
                bridge = MainAgentBridge(state, harness.session_id, report=reports.append)
                result = await bridge.handle(json.dumps([{"speaker": "user", "text": "ROOT"}]))
                assert reports[-1]["status"] == expected
                if wrong_parent:
                    assert "could not complete" in result and "result_text" not in reports[-1]
                else:
                    assert result == reports[-1]["result_text"] == "ROOT_RECOVERED"
            else:
                response = await client.post(
                    "/api/run", headers=headers, json={"session_id": harness.session_id, "prompt": "ROOT"}
                )
                assert response.status_code == 200
                wire = [json.loads(line) for line in response.text.splitlines() if line]
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
            assert error["recoverable"] is False and "task_id" not in error and "code" not in error
            assert not protected_drafts
        else:
            assert error["recoverable"] is True and error["code"] == "TASK_DELIVERY_SKIPPED"
            assert "parent stopped" in str(error["message"]) and "Provider" not in str(error["message"])
            for key in (
                "task_id",
                "parent_task_id",
                "parent_session_id",
                "child_session_id",
                "depth",
                "activation",
                "followup",
            ):
                assert error[key] == descendant[key]
            assert "extra" not in error and protected_drafts
        assert not any(
            record.get("source_task_id") == descendant["task_id"]
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
    config = HarnessConfig(tmp_path, data_dir=tmp_path / "state", auth="api-key")
    async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
        harness = harnesses[0]
        harness.run = Harness.run.__get__(harness)  # type: ignore[method-assign]
        harness.agent.provider = ScriptedProvider(
            [
                [
                    ToolCallProgressEvent(generation_id="root", index=0, name="read_file", arguments_text="{"),
                    ErrorEvent(
                        code=code,
                        message="PRIVATE_UPSTREAM_DETAIL",
                        extra={"task_id": "forged-child", "activation": 123, "password": "PRIVATE_UPSTREAM_DETAIL"},
                    ),
                ]
            ]
        )
        async with asyncio.timeout(HANG_GUARD):
            response = await client.post(
                "/api/run", headers=headers, json={"session_id": harness.session_id, "prompt": "ROOT"}
            )
        assert response.status_code == 200 and "PRIVATE_UPSTREAM_DETAIL" not in response.text
        records = [json.loads(line) for line in response.text.splitlines() if line]
        error = next(record for record in records if record["event"] == "error")
        assert error["recoverable"] is False
        assert not {"extra", "task_id", "activation", "password", "code"} & error.keys()
        assert next(record for record in records if record["event"] == "run_finished")["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True])
async def test_stop_still_cancels_chat_and_voice_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, voice: bool
) -> None:
    scenario = ChildFailureScenario(monkeypatch, hold_child=True)
    config = HarnessConfig(tmp_path, data_dir=tmp_path / "state", auth="api-key", max_tool_rounds=6)
    async with client_app(tmp_path, config=config, controlled=False) as (app, client, headers, harnesses):
        harness = harnesses[0]
        state = cast("WebState", app.state.web)
        reports: list[dict[str, object]] = []
        async with asyncio.timeout(HANG_GUARD):
            if voice:
                bridge = MainAgentBridge(state, harness.session_id, report=reports.append)
                speech = asyncio.create_task(bridge.handle(json.dumps([{"speaker": "user", "text": "ROOT"}])))
            else:
                chat = asyncio.create_task(
                    client.post("/api/run", headers=headers, json={"session_id": harness.session_id, "prompt": "ROOT"})
                )
            await scenario.grandchild_started.wait()
            await scenario.root_preview.wait()
            assert state.active is not None
            response = await client.post("/api/cancel", headers=headers, json={"run_id": state.active.id})
            assert response.status_code == 200
            if voice:
                assert "stopped" in await speech
                assert reports[-1]["status"] == "cancelled" and "result_text" not in reports[-1]
            else:
                response = await chat
                records = [json.loads(line) for line in response.text.splitlines() if line]
                assert next(record for record in records if record["event"] == "run_finished")["status"] == "cancelled"
        assert {info.status for info in harness.tasks.list()} == {"cancelled"}
        assert len(scenario.providers) == 3 and all(provider.closed for provider in scenario.providers[1:])
        assert "DESCENDANT_PRIVATE" not in str(scenario.providers[0].requests)
        assert state.active is None
