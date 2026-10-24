"""Optional web display sidecar: never part of model history or voice context."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from collections.abc import Mapping

    import aiosqlite

    from nagents.events import ResponseMetadataEvent

SUMMARY_LIMIT = 16384
SUMMARY_RETENTION = 100
_response_write: ContextVar[Mapping[str, object]] = ContextVar("ngn_response_write", default=MappingProxyType({}))


@contextmanager
def response_write(snapshot: ResponseMetadataEvent) -> Iterator[None]:
    payload = {key: value for key, value in asdict(snapshot).items() if key not in {"type", "timestamp", "extra"}}
    payload["event"] = "response_metadata"
    token = _response_write.set(payload)
    try:
        yield
    finally:
        _response_write.reset(token)


async def anchor_response(db: aiosqlite.Connection, session: str, history_id: int) -> None:
    """Bind the exact assistant insert in its transaction, if web owns a sidecar."""
    payload = _response_write.get()
    if not payload:
        return
    cursor = await db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ngn_response_metadata' AND type = 'table'")
    if await cursor.fetchone() is not None:
        await db.execute(
            "INSERT INTO ngn_response_metadata(session_id,response_id,history_id,anchor_id,revision,status,payload) "
            "VALUES (?, ?, ?, ?, ?, 'completed', ?) ON CONFLICT(session_id,response_id) DO UPDATE SET "
            "history_id = excluded.history_id, status = 'completed', revision = excluded.revision, "
            "payload = json_patch(payload, excluded.payload) WHERE history_id = 0 OR history_id = excluded.history_id",
            (
                session,
                str(payload["response_id"]),
                history_id,
                history_id,
                int(str(payload["revision"])),
                json.dumps(payload),
            ),
        )
        await db.execute(
            "UPDATE ngn_response_metadata SET revision = revision + 1, payload = json_set(payload, '$.summary', '', '$.summary_expired', json('true')) "
            "WHERE session_id = ? AND COALESCE(json_extract(payload, '$.summary_expired'), 0) != 1 AND sequence NOT IN "
            "(SELECT sequence FROM ngn_response_metadata WHERE session_id = ? ORDER BY sequence DESC LIMIT ?)",
            (session, session, SUMMARY_RETENTION),
        )


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS ngn_response_metadata ("
        "sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, response_id TEXT NOT NULL, "
        "history_id INTEGER NOT NULL DEFAULT 0, anchor_id INTEGER NOT NULL DEFAULT 0, "
        "revision INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, "
        "UNIQUE(session_id, response_id))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS ngn_response_metadata_session ON ngn_response_metadata(session_id, sequence)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS ngn_response_metadata_run ON ngn_response_metadata(session_id, json_extract(payload, '$.run_id'))"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_response_metadata_message_deleted BEFORE DELETE ON v2_messages BEGIN "
        "DELETE FROM ngn_response_metadata WHERE history_id = OLD.id OR (history_id = 0 AND anchor_id = OLD.id); END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_response_metadata_session_deleted BEFORE DELETE ON v2_sessions BEGIN "
        "DELETE FROM ngn_response_metadata WHERE session_id = OLD.id; END"
    )


def save(db: sqlite3.Connection, record: dict[str, object]) -> dict[str, object]:
    session, response = str(record["response_session_id"]), str(record["response_id"])
    if db.execute("SELECT 1 FROM v2_sessions WHERE id = ?", (session,)).fetchone() is None:
        return record
    existing = db.execute(
        "SELECT history_id, revision, status FROM ngn_response_metadata WHERE session_id = ? AND response_id = ?",
        (session, response),
    ).fetchone()
    revision = int(str(record["revision"]))
    history = int(str(record.get("history_id") or 0))
    if existing and (int(existing[1]) > revision or (existing[2] == "completed" and record["status"] != "completed")):
        # A cancellation may arrive after an atomic assistant insert whose
        # observer envelope was still queued. Fill only trusted identity fields;
        # never replace its completed status, exact row, summary or usage.
        previous = lookup(db, session, response)
        scope: dict[str, object] = {
            key: record[key]
            for key in ("run_id", "session_id", "activation", "followup", "turn_id")
            if key in record and key not in previous
        }
        if scope:
            previous.update(scope)
            db.execute(
                "UPDATE ngn_response_metadata SET payload = ? WHERE session_id = ? AND response_id = ?",
                (json.dumps(previous), session, response),
            )
        return previous
    if (
        history
        and db.execute(
            "SELECT 1 FROM v2_messages WHERE id = ? AND session_id = ? AND role = 'assistant'", (history, session)
        ).fetchone()
        is None
    ):
        history = 0
    anchor = db.execute("SELECT COALESCE(MAX(id), 0) FROM v2_messages WHERE session_id = ?", (session,)).fetchone()[0]
    payload = {**record, "summary": str(record.get("summary", ""))[:SUMMARY_LIMIT]}
    db.execute(
        "INSERT INTO ngn_response_metadata(session_id,response_id,history_id,anchor_id,revision,status,payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id,response_id) DO UPDATE SET "
        "history_id = MAX(history_id, excluded.history_id), revision = excluded.revision, "
        "status = excluded.status, payload = excluded.payload WHERE excluded.revision >= revision",
        (session, response, history, anchor, revision, str(record["status"]), json.dumps(payload)),
    )
    db.execute(
        "UPDATE ngn_response_metadata SET revision = revision + 1, payload = json_set(payload, '$.summary', '', '$.summary_expired', json('true')) "
        "WHERE session_id = ? AND COALESCE(json_extract(payload, '$.summary_expired'), 0) != 1 AND sequence NOT IN (SELECT sequence FROM ngn_response_metadata "
        "WHERE session_id = ? ORDER BY sequence DESC LIMIT ?)",
        (session, session, SUMMARY_RETENTION),
    )
    return lookup(db, session, response)


def _record(row: tuple[object, ...]) -> dict[str, object]:
    history, revision, status, raw = row
    payload = json.loads(str(raw))
    assert isinstance(payload, dict)
    return {**payload, "history_id": str(history) if history else "", "revision": revision, "status": status}


def lookup(db: sqlite3.Connection, session: str, response: str) -> dict[str, object]:
    row = db.execute(
        "SELECT history_id, revision, status, payload FROM ngn_response_metadata WHERE session_id = ? AND response_id = ?",
        (session, response),
    ).fetchone()
    return _record(tuple(row)) if row else {}


def merge(
    db: sqlite3.Connection,
    session: str,
    records: list[dict[str, object]],
    *,
    orphans: bool = True,
    maximum: int = 2**63 - 1,
) -> list[dict[str, object]]:
    indexed = {str(record.get("history_id", "")): record for record in records}
    totals: dict[str, dict[str, object]] = {}
    orphan_records: list[tuple[int, dict[str, object]]] = []
    boundary = db.execute(
        "SELECT COALESCE(compacted_at_message_id, 0) FROM v2_sessions WHERE id = ?", (session,)
    ).fetchone()
    minimum = int(boundary[0]) if boundary else 0
    for history, revision, status, payload, anchor in db.execute(
        "SELECT history_id, revision, status, payload, anchor_id FROM ngn_response_metadata "
        "WHERE session_id = ? ORDER BY sequence",
        (session,),
    ):
        metadata = _record((history, revision, status, payload))
        run_id = str(metadata.get("run_id", ""))
        if run_id:
            if run_id not in totals:
                totals[run_id] = run_totals(db, session, run_id)
            metadata["run_usage"] = totals[run_id]
        if str(history) in indexed:
            indexed[str(history)].update(response_id=metadata["response_id"], response_metadata=metadata)
        elif orphans and not history and minimum <= int(anchor) <= maximum:
            orphan_records.append(
                (
                    int(anchor),
                    {
                        "role": "response_metadata",
                        "history_id": f"response-{metadata['response_id']}",
                        "content": "",
                        "name": "",
                        "tool_call_id": "",
                        "tool_calls": [],
                        "response_metadata": metadata,
                    },
                )
            )
    result: list[dict[str, object]] = []
    for record in records:
        identifier = str(record.get("history_id", ""))
        if identifier.isdecimal():
            preceding = [(anchor, orphan) for anchor, orphan in orphan_records if anchor < int(identifier)]
            result.extend(orphan for _, orphan in preceding)
            orphan_records = [(anchor, orphan) for anchor, orphan in orphan_records if anchor >= int(identifier)]
        result.append(record)
    result.extend(orphan for _, orphan in orphan_records)
    return result


def fork(db: sqlite3.Connection, source: str, target: str, ids: dict[int, int]) -> None:
    rows = db.execute(
        "SELECT history_id, anchor_id, revision, status, payload FROM ngn_response_metadata WHERE session_id = ? ORDER BY sequence",
        (source,),
    ).fetchall()
    runs: dict[str, str] = {}
    for history, anchor, revision, status, raw in rows:
        if (history and history not in ids) or (anchor and anchor not in ids):
            continue
        response = uuid4().hex
        payload = json.loads(raw)
        payload.update(
            response_id=response,
            response_session_id=target,
            run_id=runs.setdefault(str(payload.get("run_id", "")), f"fork-{uuid4().hex}"),
            history_id=str(ids.get(history, 0)) if history else "",
        )
        db.execute(
            "INSERT INTO ngn_response_metadata(session_id,response_id,history_id,anchor_id,revision,status,payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target, response, ids.get(history, 0), ids.get(anchor, 0), revision, status, json.dumps(dict(payload))),
        )


def run_totals(db: sqlite3.Connection, session: str, run_id: str) -> dict[str, object]:
    rows = db.execute(
        "SELECT payload FROM ngn_response_metadata WHERE session_id = ? AND json_extract(payload, '$.run_id') = ?",
        (session, run_id),
    ).fetchall()
    total: dict[str, object] = {
        key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")
    }
    usage_count = reasoning_count = 0
    coverage = {"prompt": 0, "completion": 0, "total": 0}
    inclusions: set[str] = set()
    for (raw,) in rows:
        record = json.loads(raw)
        usage = record.get("usage", {})
        if not record.get("usage_reported") or not isinstance(usage, dict):
            continue
        usage_count += 1
        for counter in coverage:
            if usage.get(f"{counter}_reported") or (
                not usage.get("reported")
                and type(usage.get(f"{counter}_tokens")) is int
                and usage[f"{counter}_tokens"] > 0
            ):
                coverage[counter] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
            value = usage.get(key, 0)
            if type(value) is int and value >= 0:
                total[key] = int(str(total[key])) + value
        if usage.get("reasoning_reported"):
            reasoning_count += 1
            inclusions.add(str(usage.get("reasoning_in_output", "unknown")))
    total.update(
        response_count=len(rows),
        usage_response_count=usage_count,
        reasoning_response_count=reasoning_count,
        complete=bool(rows) and coverage["prompt"] == len(rows) and coverage["completion"] == len(rows),
        prompt_reported=coverage["prompt"] > 0,
        completion_reported=coverage["completion"] > 0,
        total_reported=coverage["total"] > 0,
        prompt_response_count=coverage["prompt"],
        completion_response_count=coverage["completion"],
        total_response_count=coverage["total"],
        reasoning_reported=bool(rows) and reasoning_count == len(rows),
        reasoning_in_output=next(iter(inclusions)) if len(inclusions) == 1 else "unknown",
    )
    return total
