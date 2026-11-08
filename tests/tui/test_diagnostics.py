"""Runtime metadata log privacy, timestamps and logger cleanup."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

import pytest

from nagents.tui.diagnostics import RuntimeLog

if TYPE_CHECKING:
    from pathlib import Path


def test_runtime_log_is_private_timestamped_metadata_and_restores_loggers(tmp_path: Path) -> None:
    tui_logger = logging.getLogger("nagents.tui")
    root_logger = logging.getLogger()
    old_level, old_propagate = tui_logger.level, tui_logger.propagate
    old_handlers, root_handlers = tuple(tui_logger.handlers), tuple(root_logger.handlers)
    with RuntimeLog(tmp_path) as runtime:
        path = runtime.path
        assert path.parent.name == "logs"
        logging.getLogger("nagents.tui.app").info(
            "run started", extra={"session_id": "session-1", "provider": "demo", "model": "offline"}
        )
        logging.getLogger("nagents.tui.app").debug("must not be recorded")
        logging.getLogger("provider.sdk").warning(
            "PRIVATE-WARNING", extra={"tool_args": "PRIVATE-ARGS", "prompt": "PRIVATE-PROMPT"}
        )
        try:
            raise ValueError("PRIVATE-EXCEPTION")
        except ValueError:
            logging.getLogger("nagents.tui.app").exception("PRIVATE-MESSAGE %s", "PRIVATE-ARGUMENT")
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert tui_logger.level == old_level and tui_logger.propagate == old_propagate
    assert tuple(tui_logger.handlers) == old_handlers
    assert tuple(root_logger.handlers) == root_handlers
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert not {"session_id", "provider", "model"} & records[0].keys()
    assert records[0]["level"] == "INFO"
    assert records[1]["level"] == "ERROR"
    assert records[1]["exception_type"] == "ValueError"
    for record in records:
        assert record["elapsed_ms"] >= 0
        assert record["time"].endswith("+00:00")
    content = path.read_text(encoding="utf-8")
    assert "PRIVATE-" not in content
    assert "must not be recorded" not in content


# Include every formerly accepted extra, plus unrecognized payload fields.
_LABELS = ("session_id", "provider", "model", "tool", "version", "value", "threshold")
_CATEGORIES = ("phase", "status", "metric")
_MEASUREMENTS = ("duration_ms", "threshold_ms", "lag_p95_ms", "lag_max_ms", "cpu_percent", "peak_memory_mb")
_COUNTS = ("sample_count", "running_tools", "running_agents", "queued_prompts", "backlog_chars")


@pytest.mark.parametrize("field", (*_LABELS, *_CATEGORIES, *_MEASUREMENTS, *_COUNTS, "prompt", "tool_args"))
def test_every_extra_rejects_payload_strings(tmp_path: Path, field: str) -> None:
    secret = f"PRIVATE-{field}-token"
    with RuntimeLog(tmp_path) as runtime:
        logging.getLogger("nagents.tui").info("event", extra={field: secret})
    content = runtime.path.read_text(encoding="utf-8")
    assert secret not in content
    assert field not in json.loads(content)


@pytest.mark.parametrize("value", ["12345", True, None, [], {}, -1, float("nan"), float("inf"), 10**1000])
def test_numeric_telemetry_rejects_invalid_values(tmp_path: Path, value: object) -> None:
    with RuntimeLog(tmp_path) as runtime:
        logging.getLogger("nagents.tui").info("event", extra=dict.fromkeys((*_MEASUREMENTS, *_COUNTS), value))
    record = json.loads(runtime.path.read_text(encoding="utf-8"))
    assert not set((*_MEASUREMENTS, *_COUNTS)) & record.keys()


def test_closed_schema_preserves_useful_telemetry(tmp_path: Path) -> None:
    extra: dict[str, object] = dict.fromkeys(_MEASUREMENTS, 12.5)
    extra.update(dict.fromkeys(_COUNTS, 3))
    extra.update(phase="performance", status="degraded", metric="callback")
    with RuntimeLog(tmp_path) as runtime:
        logging.getLogger("nagents.tui").warning("event", extra=extra)
    record = json.loads(runtime.path.read_text(encoding="utf-8"))
    assert all(record[key] == value for key, value in extra.items())
    assert record["level"] == "WARNING"


def test_names_messages_and_exception_details_are_not_payload_channels(tmp_path: Path) -> None:
    secret = "PRIVATE-NAME-token"
    # Spoof a built-in class name too: only exact built-in identities are safe.
    exceptions = (type(secret, (ValueError,), {}), type("ValueError", (ValueError,), {}))
    with RuntimeLog(tmp_path) as runtime:
        for exception in exceptions:
            record = logging.LogRecord(
                f"nagents.tui.{secret}",
                40,
                secret,
                1,
                secret + " %s",
                (secret,),
                (exception, exception(secret), None),
                func=secret,
                sinfo=secret,
            )
            record.levelname = secret
            record.exc_text = secret
            record.__dict__["exception_type"] = secret
            runtime.handler.handle(record)
        record = logging.LogRecord(secret, 123, secret, 1, secret, (), None)
        record.levelname = secret
        runtime.handler.handle(record)
    content = runtime.path.read_text(encoding="utf-8")
    assert secret not in content
    records = [json.loads(line) for line in content.splitlines()]
    assert [record.get("exception_type") for record in records] == ["other", "other", None]
    assert [record["level"] for record in records] == ["ERROR", "ERROR", "OTHER"]
    assert all(record["logger"] == "nagents.tui" for record in records)


@pytest.mark.parametrize("value", [123, 12.5, True, "ordinary-identifier", "1.2.3"])
def test_identity_labels_are_omitted_regardless_of_value(tmp_path: Path, value: object) -> None:
    with RuntimeLog(tmp_path) as runtime:
        logging.getLogger("nagents.tui").info("event", extra=dict.fromkeys(_LABELS, value))
    assert not set(_LABELS) & json.loads(runtime.path.read_text(encoding="utf-8")).keys()
