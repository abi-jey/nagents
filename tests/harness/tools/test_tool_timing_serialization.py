"""Timing telemetry uses the shared CLI and web event wire format."""

import json

import pytest

from nagents.cli import _event_record
from nagents.harness.types import ToolApprovalTiming
from nagents.web.execution.delivery_transcript import DeliveryTranscript


@pytest.mark.parametrize("waiting", [True, False])
@pytest.mark.parametrize("web", [False, True])
def test_approval_timing_serialization(waiting: bool, web: bool) -> None:
    event = ToolApprovalTiming("call-id", 1.25, waiting)
    record = DeliveryTranscript().record(event) if web else _event_record(event)
    assert json.loads(json.dumps(record)) == {
        "schema_version": 1,
        "event": "tool_approval_timing",
        "call_id": "call-id",
        "elapsed": 1.25,
        "waiting": waiting,
    }
