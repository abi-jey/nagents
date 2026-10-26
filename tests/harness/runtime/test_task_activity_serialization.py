"""Child inspection stays a nested record, never a root answer or tool call."""

import json

import pytest

from nagents.cli import _event_record
from nagents.harness.types import TaskActivity


@pytest.mark.parametrize("kind", ["text_done", "tool_call", "tool_result", "error"])
def test_task_activity_preserves_host_scope_without_becoming_root_output(kind: str) -> None:
    event = TaskActivity(
        session_id="s2",
        root_session_id="ngn-root",
        parent_session_id="s1",
        depth=2,
        activation=3,
        sequence=11,
        timestamp=1_792_000_000.0,
        turn_id="child-turn",
        payload={"type": kind, "text": "Child evidence", "id": "shared-tool-id"},
    )
    record = _event_record(event)
    assert record["event"] == "task_activity"
    assert record["schema_version"] == 1
    assert record["session_id"] == "s2"
    assert record["parent_session_id"] == "s1"
    assert record["activation"] == 3
    assert record["sequence"] == 11
    assert "text" not in record
    assert "final_text" not in record
    assert "id" not in record
    assert json.loads(json.dumps(record))["payload"]["type"] == kind
    # Serialization detaches the payload from the host-owned event as well.
    payload = record["payload"]
    assert isinstance(payload, dict)
    payload["text"] = "Changed by a consumer"
    assert event.payload["text"] == "Child evidence"
