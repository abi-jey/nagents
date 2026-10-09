"""Workspace chat folders: metadata only, independent of execution ownership."""

from __future__ import annotations

import secrets
import unicodedata
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from .routing import RoutingStore

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from fastapi import FastAPI

MAX_GROUPS = 128
MAX_GROUP_DEPTH = 12


class GroupRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=r"^[a-f0-9]{32}$")


class GroupCreate(GroupRevision):
    name: str = Field(min_length=1, max_length=80)
    parent_id: str = Field(default="", pattern=r"^(?:group-[a-f0-9]{32})?$")


class GroupTarget(GroupRevision):
    group_id: str = Field(pattern=r"^group-[a-f0-9]{32}$")


class GroupUpdate(GroupTarget):
    name: str = Field(min_length=1, max_length=80)
    collapsed: bool


class GroupReparent(GroupTarget):
    parent_id: str = Field(pattern=r"^(?:group-[a-f0-9]{32})?$")


class GroupMove(GroupRevision):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    group_id: str = Field(default="", pattern=r"^(?:group-[a-f0-9]{32})?$")


def _name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 80 or any(unicodedata.category(char) in {"Cc", "Cs"} for char in name):
        raise HTTPException(422, "Choose a folder name of 1-80 characters without control characters.")
    return name


class SessionGroups:
    def __init__(self, store: RoutingStore) -> None:
        self.store = store

    async def initialize(self) -> None:
        def initialize(db: sqlite3.Connection) -> None:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_web_group_revision ("
                "id INTEGER PRIMARY KEY CHECK(id = 1), revision TEXT NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO ngn_web_group_revision VALUES (1, ?)", (secrets.token_hex(16),))
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_web_session_groups ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL, "
                "collapsed INTEGER NOT NULL DEFAULT 0 CHECK(collapsed IN (0, 1)), "
                "parent_id TEXT NOT NULL DEFAULT '', UNIQUE(parent_id, name_key))"
            )
            if "parent_id" not in {row[1] for row in db.execute("PRAGMA table_info(ngn_web_session_groups)")}:
                db.execute("ALTER TABLE ngn_web_session_groups ADD COLUMN parent_id TEXT NOT NULL DEFAULT ''")
            legacy_unique = any(
                bool(index[2])
                and [row[0] for row in db.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],))]
                == ["name_key"]
                for index in db.execute("PRAGMA index_list(ngn_web_session_groups)").fetchall()
            )
            if legacy_unique:
                db.execute(
                    "CREATE TABLE ngn_web_session_groups_v2 (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                    "name_key TEXT NOT NULL, collapsed INTEGER NOT NULL DEFAULT 0 CHECK(collapsed IN (0, 1)), "
                    "parent_id TEXT NOT NULL DEFAULT '', UNIQUE(parent_id, name_key))"
                )
                db.execute(
                    "INSERT INTO ngn_web_session_groups_v2 SELECT id, name, name_key, collapsed, parent_id "
                    "FROM ngn_web_session_groups"
                )
                db.execute("DROP TABLE ngn_web_session_groups")
                db.execute("ALTER TABLE ngn_web_session_groups_v2 RENAME TO ngn_web_session_groups")
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_web_group_members (session_id TEXT PRIMARY KEY, group_id TEXT NOT NULL)"
            )
            # Older ngn versions may have removed chats without knowing folders.
            # Soft-deleted chats retain v2_sessions, so their membership survives.
            db.execute(
                "DELETE FROM ngn_web_group_members WHERE NOT EXISTS ("
                "SELECT 1 FROM v2_sessions s WHERE s.id = session_id) OR NOT EXISTS ("
                "SELECT 1 FROM ngn_web_session_groups g WHERE g.id = group_id)"
            )

        await self.store._transaction(initialize)

    @staticmethod
    def _snapshot(db: sqlite3.Connection) -> dict[str, object]:
        revision = db.execute("SELECT revision FROM ngn_web_group_revision WHERE id = 1").fetchone()[0]
        groups = [
            {"id": group_id, "name": name, "collapsed": bool(collapsed), "parent_id": parent_id}
            for group_id, name, collapsed, parent_id in db.execute(
                "SELECT id, name, collapsed, parent_id FROM ngn_web_session_groups ORDER BY name_key, id"
            )
        ]
        memberships = dict(
            db.execute(
                "SELECT m.session_id, m.group_id FROM ngn_web_group_members m "
                "JOIN harness_sessions h ON h.id = m.session_id "
                "JOIN v2_sessions s ON s.id = h.id JOIN ngn_web_session_groups g ON g.id = m.group_id"
            )
        )
        return {"revision": revision, "groups": groups, "memberships": memberships}

    @staticmethod
    def _revise(db: sqlite3.Connection, revision: str) -> None:
        changed = db.execute(
            "UPDATE ngn_web_group_revision SET revision = ? WHERE id = 1 AND revision = ?",
            (secrets.token_hex(16), revision),
        ).rowcount
        if changed != 1:
            raise HTTPException(409, "Chat folders changed. Refresh before trying again.")

    @staticmethod
    def _find(db: sqlite3.Connection, group_id: str) -> None:
        if db.execute("SELECT 1 FROM ngn_web_session_groups WHERE id = ?", (group_id,)).fetchone() is None:
            raise HTTPException(404, "Chat folder no longer exists.")

    @classmethod
    def _parent(cls, db: sqlite3.Connection, group_id: str, parent_id: str) -> None:
        if parent_id:
            cls._find(db, parent_id)
        parents = dict(db.execute("SELECT id, parent_id FROM ngn_web_session_groups"))
        parents[group_id] = parent_id
        for node in parents:
            visited: set[str] = set()
            current = node
            while current:
                if current in visited:
                    raise HTTPException(422, "A folder cannot be placed inside itself or one of its subfolders.")
                visited.add(current)
                if len(visited) > MAX_GROUP_DEPTH:
                    raise HTTPException(422, f"Folders can be nested at most {MAX_GROUP_DEPTH} levels deep.")
                if current not in parents:
                    raise HTTPException(409, "The folder tree has a missing parent. Refresh before trying again.")
                current = parents[current]

    async def snapshot(self) -> dict[str, object]:
        return await self.store._transaction(self._snapshot)

    async def save(self, body: GroupCreate | GroupUpdate) -> dict[str, object]:
        name = _name(body.name)

        def save(db: sqlite3.Connection) -> dict[str, object]:
            self._revise(db, body.revision)
            group_id = body.group_id if isinstance(body, GroupUpdate) else "group-" + secrets.token_hex(16)
            if isinstance(body, GroupUpdate):
                self._find(db, group_id)
                parent_id = str(
                    db.execute("SELECT parent_id FROM ngn_web_session_groups WHERE id = ?", (group_id,)).fetchone()[0]
                )
            else:
                parent_id = body.parent_id
            if db.execute(
                "SELECT 1 FROM ngn_web_session_groups WHERE name_key = ? AND parent_id = ? AND id != ?",
                (name.casefold(), parent_id, group_id),
            ).fetchone():
                raise HTTPException(409, "A folder with that name already exists at this level.")
            if isinstance(body, GroupUpdate):
                db.execute(
                    "UPDATE ngn_web_session_groups SET name = ?, name_key = ?, collapsed = ? WHERE id = ?",
                    (name, name.casefold(), int(body.collapsed), group_id),
                )
            else:
                if db.execute("SELECT COUNT(*) FROM ngn_web_session_groups").fetchone()[0] >= MAX_GROUPS:
                    raise HTTPException(409, f"This workspace already has {MAX_GROUPS} chat folders.")
                self._parent(db, group_id, body.parent_id)
                db.execute(
                    "INSERT INTO ngn_web_session_groups (id, name, name_key, collapsed, parent_id) VALUES (?, ?, ?, 0, ?)",
                    (group_id, name, name.casefold(), body.parent_id),
                )
            return self._snapshot(db)

        return await self.store._transaction(save)

    async def reparent(self, body: GroupReparent) -> dict[str, object]:
        def reparent(db: sqlite3.Connection) -> dict[str, object]:
            self._revise(db, body.revision)
            self._find(db, body.group_id)
            self._parent(db, body.group_id, body.parent_id)
            if db.execute(
                "SELECT 1 FROM ngn_web_session_groups WHERE parent_id = ? AND id != ? AND name_key = "
                "(SELECT name_key FROM ngn_web_session_groups WHERE id = ?)",
                (body.parent_id, body.group_id, body.group_id),
            ).fetchone():
                raise HTTPException(409, "A folder with that name already exists in the destination.")
            db.execute("UPDATE ngn_web_session_groups SET parent_id = ? WHERE id = ?", (body.parent_id, body.group_id))
            return self._snapshot(db)

        return await self.store._transaction(reparent)

    async def move(self, body: GroupMove) -> dict[str, object]:
        def move(db: sqlite3.Connection) -> dict[str, object]:
            self._revise(db, body.revision)
            RoutingStore.root(db, body.session_id)
            if body.group_id:
                self._find(db, body.group_id)
                db.execute(
                    "INSERT INTO ngn_web_group_members VALUES (?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET group_id = excluded.group_id",
                    (body.session_id, body.group_id),
                )
            else:
                db.execute("DELETE FROM ngn_web_group_members WHERE session_id = ?", (body.session_id,))
            return self._snapshot(db)

        return await self.store._transaction(move)

    async def remove(self, body: GroupTarget) -> dict[str, object]:
        def remove(db: sqlite3.Connection) -> dict[str, object]:
            self._revise(db, body.revision)
            self._find(db, body.group_id)
            parent = str(
                db.execute("SELECT parent_id FROM ngn_web_session_groups WHERE id = ?", (body.group_id,)).fetchone()[0]
            )
            if db.execute(
                "SELECT 1 FROM ngn_web_session_groups child JOIN ngn_web_session_groups sibling "
                "ON sibling.parent_id = ? AND sibling.name_key = child.name_key "
                "WHERE child.parent_id = ? AND sibling.id != ?",
                (parent, body.group_id, body.group_id),
            ).fetchone():
                raise HTTPException(409, "Rename or move conflicting subfolders before removing this folder.")
            # A child may share this folder's name. Remove the parent before
            # promoting children so its sibling-name constraint is released.
            db.execute("DELETE FROM ngn_web_session_groups WHERE id = ?", (body.group_id,))
            db.execute("UPDATE ngn_web_session_groups SET parent_id = ? WHERE parent_id = ?", (parent, body.group_id))
            if parent:
                db.execute("UPDATE ngn_web_group_members SET group_id = ? WHERE group_id = ?", (parent, body.group_id))
            else:
                db.execute("DELETE FROM ngn_web_group_members WHERE group_id = ?", (body.group_id,))
            return self._snapshot(db)

        return await self.store._transaction(remove)


def register_session_groups(app: FastAPI, service: Callable[[], SessionGroups]) -> None:
    @app.get("/api/session-groups")
    async def snapshot() -> dict[str, object]:
        return await service().snapshot()

    @app.post("/api/session-groups/create")
    async def create(body: GroupCreate) -> dict[str, object]:
        return await service().save(body)

    @app.post("/api/session-groups/update")
    async def update(body: GroupUpdate) -> dict[str, object]:
        return await service().save(body)

    @app.post("/api/session-groups/move")
    async def move(body: GroupMove) -> dict[str, object]:
        return await service().move(body)

    @app.post("/api/session-groups/reparent")
    async def reparent(body: GroupReparent) -> dict[str, object]:
        return await service().reparent(body)

    @app.post("/api/session-groups/remove")
    async def remove(body: GroupTarget) -> dict[str, object]:
        return await service().remove(body)
