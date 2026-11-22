"""Round-trip the shared protocol fixtures and enforce the generated-client boundary."""

import json
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from nagents.protocol import PROTOCOL_VERSION
from nagents.protocol import models
from nagents.protocol.events import EventRecord
from nagents.protocol.events import History
from nagents.protocol.models import ApprovalDecision
from nagents.protocol.models import EngineConfig
from nagents.protocol.models import SessionCreate
from nagents.protocol.models import SessionFork

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/engine/v1"
FIXTURES = json.loads((CONTRACT / "fixtures.json").read_text(encoding="utf-8"))


def test_all_http_message_fixtures_roundtrip() -> None:
    classes = {
        name: cls
        for name, cls in vars(models).items()
        if isinstance(cls, type) and issubclass(cls, models.WireModel) and cls.model_fields
    }
    classes["History"] = History
    assert set(FIXTURES["messages"]) == classes.keys()
    for name, fixture in FIXTURES["messages"].items():
        assert json.loads(classes[name].model_validate(fixture).model_dump_json()) == fixture, name


def test_all_event_fixtures_roundtrip() -> None:
    kinds = EventRecord.model_json_schema()["$defs"]["EngineEvent"]["discriminator"]["mapping"]
    tags = [content["type"] for content in FIXTURES["events"]]
    assert sorted(tags) == sorted(kinds)
    for seq, event in enumerate(FIXTURES["events"], 1):
        envelope = {
            "schema_version": PROTOCOL_VERSION,
            "sequence": seq,
            "session_id": "session-1",
            "run_id": "run-1",
            "timestamp_ms": 1_735_689_600_000,
            "event": event,
        }
        assert json.loads(EventRecord.model_validate(envelope).model_dump_json()) == envelope


def test_nullable_and_omitted_fields_have_different_wire_values() -> None:
    assert SessionFork.model_validate({}).model_dump(exclude_unset=True) == {}
    assert SessionFork.model_validate({"title": None}).model_dump(exclude_unset=True) == {"title": None}
    assert SessionCreate.model_validate({}).title == "New session"
    with pytest.raises(ValidationError):
        SessionCreate.model_validate({"title": None})
    config = dict(FIXTURES["messages"]["EngineConfig"])
    assert EngineConfig.model_validate(config).model is None
    del config["model"]
    with pytest.raises(ValidationError):
        EngineConfig.model_validate(config)


def test_requests_cannot_silently_drop_unknown_fields_or_coerce_decisions() -> None:
    valid = FIXTURES["messages"]["ApprovalDecision"]
    with pytest.raises(ValidationError):
        ApprovalDecision.model_validate({**valid, "agent": "unexpected"})
    with pytest.raises(ValidationError):
        ApprovalDecision.model_validate({**valid, "allow": "false"})


def test_event_version_is_required_and_pinned() -> None:
    record = dict(FIXTURES["messages"]["History"]["events"][0])
    del record["schema_version"]
    with pytest.raises(ValidationError):
        EventRecord.model_validate(record)
    with pytest.raises(ValidationError):
        EventRecord.model_validate({**record, "schema_version": PROTOCOL_VERSION + 1})


def test_generated_client_has_no_in_repository_runtime_dependency() -> None:
    manifest = tomllib.loads((ROOT / "ngn/crates/ngn-api/Cargo.toml").read_text(encoding="utf-8"))
    workspace = tomllib.loads((ROOT / "ngn/Cargo.toml").read_text(encoding="utf-8"))
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name, options in manifest.get(section, {}).items():
            if isinstance(options, dict) and options.get("workspace"):
                options = workspace["workspace"]["dependencies"][name]
            assert not isinstance(options, dict) or "path" not in options, name
