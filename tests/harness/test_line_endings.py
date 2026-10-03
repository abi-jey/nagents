"""Numbered file views expose exact newline requirements without changing bytes."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.types import ToolCall
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness import ApprovalRequest
    from nagents.types import Message
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


async def unused_provider(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
    yield TextDoneEvent(text="Unused")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "style", "counts"),
    [
        (b"", "none", {"LF": 0, "CRLF": 0, "CR": 0}),
        (b"single line", "none", {"LF": 0, "CRLF": 0, "CR": 0}),
        (b"first\nsecond\n", "LF", {"LF": 2, "CRLF": 0, "CR": 0}),
        (b"first\r\nsecond\r\n", "CRLF", {"LF": 0, "CRLF": 2, "CR": 0}),
        (b"first\rsecond\r", "CR", {"LF": 0, "CRLF": 0, "CR": 2}),
        (b"first\r\nsecond\nthird\rfourth", "mixed", {"LF": 1, "CRLF": 1, "CR": 1}),
        (b"first\r\r\nsecond\n", "mixed", {"LF": 1, "CRLF": 1, "CR": 1}),
    ],
)
async def test_read_reports_original_cr_lf_style_without_mutating_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    style: str,
    counts: dict[str, int],
) -> None:
    path = tmp_path / "fixture.txt"
    path.write_bytes(data)
    harness, _ = setup_harness(tmp_path, monkeypatch, unused_provider)
    try:
        await harness.initialize()
        result = await harness.tools.read_file("fixture.txt")
        assert result["newline_style"] == style
        assert result["newline_counts"] == counts
        assert result["sha256"] == hashlib.sha256(data).hexdigest()
        assert harness.tools.read_hashes["fixture.txt"] == result["sha256"]
        assert "\r" not in str(result["content"])
        assert path.read_bytes() == data
        assert len(json.dumps({"style": result["newline_style"], "counts": result["newline_counts"]})) < 100
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_slice_and_truncation_keep_whole_file_newline_metadata_and_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"x" * 3000 + b"\r\nsecond\r\n"
    (tmp_path / "fixture.txt").write_bytes(data)
    harness, _ = setup_harness(tmp_path, monkeypatch, unused_provider)
    harness.config.max_output = 1024
    try:
        await harness.initialize()
        full = await harness.tools.read_file("fixture.txt")
        sliced = await harness.tools.read_file("fixture.txt", start_line=2, limit=1)
        assert len(str(full["content"]).encode("utf-8")) <= 1024 and full["truncated"] is True
        assert sliced["content"] == "2: second"
        assert sliced["truncated"] is False
        for result in (full, sliced):
            assert result["newline_style"] == "CRLF"
            assert result["newline_counts"] == {"LF": 0, "CRLF": 2, "CR": 0}
            assert result["sha256"] == hashlib.sha256(data).hexdigest()
        definition = harness.agent.tool_registry.get("read_file")
        assert definition is not None
        assert "1024 bytes" in definition.description
        assert "newline_style" in definition.description and "entire file" in definition.description
        assert "\\r\\n" in definition.description
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_normal_harness_recovers_from_displayed_lf_to_exact_crlf_before_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = b"untouched header\r\nfirst = 1\r\nsecond = 2\r\nuntouched tail\r\n"
    expected = b"untouched header\r\nfirst = 10\r\nsecond = 20\r\nuntouched tail\r\n"
    path = tmp_path / "fixture.txt"
    path.write_bytes(original)
    approvals: list[ApprovalRequest] = []
    exact_old = "first = 1\r\nsecond = 2"
    exact_new = "first = 10\r\nsecond = 20"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(
                id="read", name="read_file", arguments={"path": "fixture.txt", "start_line": 2, "limit": 2}
            )
        elif len(provider.requests) == 2:
            assert "'newline_style': 'CRLF'" in str(messages[-1].content)
            yield ToolCallEvent(
                id="displayed-lf",
                name="edit",
                arguments={"path": "fixture.txt", "old": "first = 1\nsecond = 2", "new": "wrong\nreplacement"},
            )
        elif len(provider.requests) == 3:
            feedback = str(messages[-1].content)
            assert "match exactly once" in feedback
            assert "newline style: CRLF" in feedback and "JSON \\r\\n" in feedback
            assert "No edit was made" in feedback
            assert approvals == [] and path.read_bytes() == original
            assert harness.tools.read_hashes["fixture.txt"] == hashlib.sha256(original).hexdigest()
            yield ToolCallEvent(
                id="exact-crlf", name="edit", arguments={"path": "fixture.txt", "old": exact_old, "new": exact_new}
            )
        else:
            assert path.read_bytes() == expected
            yield TextDoneEvent(text="Updated the exact lines while preserving the file's CRLF endings.")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    async def approve(request: ApprovalRequest) -> bool:
        assert request.tool == "edit"
        assert request.arguments == {"path": "fixture.txt", "old": exact_old, "new": exact_new}
        assert path.read_bytes() == original
        approvals.append(request)
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness, "Update the two values in fixture.txt.")
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        assert len(results) == 3
        assert results[0].name == "read_file" and not results[0].error
        assert results[1].id == "displayed-lf" and results[1].error
        assert results[2].id == "exact-crlf" and not results[2].error
        assert len(approvals) == 1 and len(providers[0].requests) == 4
        assert isinstance(events[-1], DoneEvent)
        assert path.read_bytes() == expected
        assert harness.tools.read_hashes["fixture.txt"] == hashlib.sha256(expected).hexdigest()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_mixed_ending_feedback_does_not_claim_a_uniform_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = b"alpha\r\nbeta\ngamma\rdelta\r\n"
    path = tmp_path / "fixture.txt"
    path.write_bytes(original)
    harness, _ = setup_harness(tmp_path, monkeypatch, unused_provider)
    approvals: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> bool:
        approvals.append(request)
        return True

    harness.approval_handler = approve
    try:
        await harness.initialize()
        read = await harness.tools.read_file("fixture.txt")
        assert read["newline_style"] == "mixed"
        failed = await harness.agent.tool_executor.execute(
            ToolCall("wrong", "edit", {"path": "fixture.txt", "old": "alpha\nbeta", "new": "changed"})
        )
        assert failed.error and "newline style: mixed" in failed.error
        assert "no single separator" in failed.error and "unique snippet within one line" in failed.error
        assert "Use JSON" not in failed.error
        assert not approvals and path.read_bytes() == original
        changed = await harness.agent.tool_executor.execute(
            ToolCall("exact", "edit", {"path": "fixture.txt", "old": "beta", "new": "BETA"})
        )
        assert not changed.error and len(approvals) == 1
        assert path.read_bytes() == b"alpha\r\nBETA\ngamma\rdelta\r\n"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_exact_edit_does_not_convert_the_callers_replacement_newlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fixture.txt"
    path.write_bytes(b"alpha\r\nbeta\r\nuntouched\r\n")
    harness, _ = setup_harness(tmp_path, monkeypatch, unused_provider)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    try:
        await harness.initialize()
        await harness.tools.read_file("fixture.txt")
        await harness.tools.edit("fixture.txt", "alpha\r\nbeta", "ALPHA\nBETA")
        assert path.read_bytes() == b"ALPHA\nBETA\r\nuntouched\r\n"
    finally:
        await harness.close()
