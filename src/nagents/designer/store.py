"""Workspace definitions and durable, bounded designer execution evidence."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

import aiosqlite

from nagents.observation import json_default

from .schema import IDENTIFIER
from .schema import parse

if TYPE_CHECKING:
    from pathlib import Path


def revision(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


class DesignStore:
    def __init__(self, workspace: Path) -> None:
        self.directory = workspace / ".ngn" / "designs"

    def path(self, name: str) -> Path:
        if not IDENTIFIER.fullmatch(name):
            raise ValueError("Invalid design identifier")
        for directory in (self.directory.parent, self.directory):
            if directory.is_symlink():
                raise ValueError("Design directories must not be symlinks")
        path = self.directory / f"{name}.yaml"
        if path.is_symlink():
            raise ValueError("Design files must not be symlinks")
        return path

    def list(self) -> list[str]:
        self.path("check")
        return sorted(path.stem for path in self.directory.glob("*.yaml") if not path.is_symlink())

    def read(self, name: str) -> dict[str, object]:
        with self.path(name).open("r", encoding="utf-8") as stream:
            source = stream.read(49153)
        design = parse(source)
        return {"source": source, "revision": revision(source), "design": design.document()}

    def save(self, source: str, expected: str) -> dict[str, object]:
        design = parse(source)
        path = self.path(design.id)
        current = str(self.read(design.id)["revision"]) if path.exists() else ""
        if current != expected:
            raise FileExistsError("Design changed on disk. Reload before saving.")
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.read(design.id)


class TraceStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS designer_runs (id TEXT PRIMARY KEY, session_id TEXT, agent TEXT, revision TEXT, design TEXT, status TEXT, created TEXT)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS designer_events (run_id TEXT, sequence INTEGER, record TEXT, PRIMARY KEY(run_id, sequence))"
            )
            await db.execute("UPDATE designer_runs SET status='interrupted' WHERE status='running'")
            await db.commit()

    async def start(self, run_id: str, session_id: str, agent: str, design: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO designer_runs VALUES (?, ?, ?, ?, ?, 'running', ?)",
                (run_id, session_id, agent, revision(design), design, datetime.now(UTC).isoformat()),
            )
            await db.commit()

    async def append(self, run_id: str, records: list[dict[str, object]]) -> None:
        if not records:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                "INSERT INTO designer_events VALUES (?, ?, ?)",
                [(run_id, record["sequence"], json.dumps(record)) for record in records],
            )
            await db.commit()

    async def finish(self, run_id: str, status: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE designer_runs SET status=? WHERE id=?", (status, run_id))
            await db.commit()

    async def runs(self) -> list[dict[str, object]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, session_id, agent, revision, status, created FROM designer_runs ORDER BY created DESC LIMIT 100"
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def read(self, run_id: str, after: int = 0) -> dict[str, object]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM designer_runs WHERE id=?", (run_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ValueError("Unknown designer run")
            async with db.execute(
                "SELECT record FROM designer_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT 200",
                (run_id, after),
            ) as cursor:
                records = [json.loads(item[0]) for item in await cursor.fetchall()]
            return {**dict(row), "events": records}

    async def delete(self, run_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM designer_events WHERE run_id=?", (run_id,))
            await db.execute("DELETE FROM designer_runs WHERE id=?", (run_id,))
            await db.commit()


class Recorder:
    def __init__(self) -> None:
        self.pending: list[dict[str, object]] = []
        self.sequence = 0
        self.bytes = 0
        self.truncated = False
        self.secrets: set[str] = set()

    def __call__(self, kind: str, data: dict[str, object]) -> None:
        if self.truncated:
            return
        encoded = json.dumps(data, default=json_default, ensure_ascii=True)
        if len(encoded) > 1024 * 1024:
            encoded = json.dumps({"truncated": True, "original_bytes": len(encoded)})
        else:
            encoded = json.dumps(self._redact(json.loads(encoded)))
        self.bytes += len(encoded)
        if self.bytes > 16 * 1024 * 1024 or self.sequence >= 10000:
            kind, encoded, self.truncated = "trace_truncated", "{}", True
        self.sequence += 1
        self.pending.append(
            {
                "sequence": self.sequence,
                "kind": kind,
                "timestamp": datetime.now(UTC).isoformat(),
                "data": json.loads(encoded),
            }
        )

    def _redact(self, value: object) -> object:
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                if secret:
                    value = value.replace(secret, "[redacted]")
                    value = value.replace(json.dumps(secret, ensure_ascii=True)[1:-1], "[redacted]")
            return value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {str(self._redact(key)): self._redact(item) for key, item in value.items()}
        return value

    async def flush(self, store: TraceStore, run_id: str) -> None:
        records, self.pending = self.pending, []
        await store.append(run_id, records)
