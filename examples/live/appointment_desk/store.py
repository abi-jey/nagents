"""Durable proposals, explicit application approval, and idempotent appointment changes."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Desk:
    def __init__(self, path: Path, customer: str = "demo-customer") -> None:
        self.path = path
        self.customer = customer
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY, customer TEXT, slot TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS drafts(customer TEXT PRIMARY KEY, revision INTEGER, action TEXT,
                    slot TEXT, booking TEXT, approved INTEGER, operation TEXT);
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, result TEXT);
                CREATE TABLE IF NOT EXISTS webhooks(id TEXT PRIMARY KEY, status TEXT);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, customer TEXT, status TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, state TEXT);
            """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def availability(self) -> str:
        """List the demo desk's offered slots and current bookings."""
        return json.dumps(self.state())

    def propose(self, action: str, slot: str = "", booking: str = "") -> str:
        if action not in {"book", "change", "cancel"}:
            raise ValueError("Unknown appointment action")
        if action != "cancel" and slot not in {"2030-08-06 14:00 UTC", "2030-08-07 14:00 UTC"}:
            raise ValueError("Choose a listed appointment slot")
        with self.transaction() as db:
            if (
                action != "book"
                and db.execute("SELECT id FROM bookings WHERE id=? AND customer=?", (booking, self.customer)).fetchone()
                is None
            ):
                raise ValueError("Appointment is unavailable for this customer")
            row = db.execute("SELECT revision FROM drafts WHERE customer=?", (self.customer,)).fetchone()
            revision = int(row[0]) + 1 if row else 1
            db.execute(
                "INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?,?,?)",
                (self.customer, revision, action, slot, booking, 0, uuid4().hex),
            )
        return json.dumps(
            {"revision": revision, "status": "awaiting application approval", "action": action, "slot": slot}
        )

    def propose_booking(self, slot: str) -> str:
        """Propose a listed slot; the customer must approve it in the application."""
        return self.propose("book", slot)

    def propose_change(self, booking_id: str, slot: str) -> str:
        """Propose a change to this customer's appointment, requiring fresh approval."""
        return self.propose("change", slot, booking_id)

    def propose_cancellation(self, booking_id: str) -> str:
        """Propose cancellation of this customer's appointment, requiring approval."""
        return self.propose("cancel", booking=booking_id)

    def approve(self, revision: int) -> None:
        # Deliberately NOT a model-callable tool. Only the application invokes it.
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE drafts SET approved=1 WHERE customer=? AND revision=?", (self.customer, revision)
            )
            if changed.rowcount != 1:
                raise ValueError("The proposal changed; review it again")

    def commit(self, revision: int) -> str:
        """Commit the exact proposal only if application approval and revision still match."""
        with self.transaction() as db:
            draft = db.execute("SELECT * FROM drafts WHERE customer=?", (self.customer,)).fetchone()
            if not draft or draft["revision"] != revision or not draft["approved"]:
                return "Not executed: current proposal needs explicit application approval."
            previous = db.execute("SELECT result FROM operations WHERE id=?", (draft["operation"],)).fetchone()
            if previous:
                return str(previous[0])
            if (
                draft["action"] != "book"
                and db.execute(
                    "SELECT id FROM bookings WHERE id=? AND customer=?", (draft["booking"], self.customer)
                ).fetchone()
                is None
            ):
                return "Not executed: this customer's appointment is no longer available."
            identifier = draft["booking"] or uuid4().hex[:12]
            try:
                if draft["action"] == "book":
                    db.execute("INSERT INTO bookings VALUES (?,?,?)", (identifier, self.customer, draft["slot"]))
                elif draft["action"] == "change":
                    db.execute(
                        "UPDATE bookings SET slot=? WHERE id=? AND customer=?",
                        (draft["slot"], identifier, self.customer),
                    )
                else:
                    db.execute("DELETE FROM bookings WHERE id=? AND customer=?", (identifier, self.customer))
            except sqlite3.IntegrityError:
                return "Not executed: that slot is no longer available."
            result = json.dumps(
                {"status": "completed", "action": draft["action"], "booking_id": identifier, "slot": draft["slot"]}
            )
            db.execute("INSERT INTO operations VALUES (?,?)", (draft["operation"], result))
            return result

    def state(self) -> dict[str, object]:
        with self.transaction() as db:
            draft = db.execute("SELECT * FROM drafts WHERE customer=?", (self.customer,)).fetchone()
            rows = db.execute("SELECT id,slot FROM bookings WHERE customer=?", (self.customer,)).fetchall()
            return {
                "slots": ["2030-08-06 14:00 UTC", "2030-08-07 14:00 UTC"],
                "draft": dict(draft) if draft else {},
                "bookings": [dict(row) for row in rows],
                "jobs": [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,status,result FROM jobs WHERE customer=? ORDER BY rowid DESC LIMIT 20",
                        (self.customer,),
                    )
                ],
            }

    def create_job(self) -> str:
        identifier = uuid4().hex
        with self.transaction() as db:
            db.execute("INSERT INTO jobs VALUES (?,?,'running','')", (identifier, self.customer))
        return identifier

    def finish_job(self, identifier: str, status: str, result: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE jobs SET status=?,result=? WHERE id=? AND customer=?",
                (status, result, identifier, self.customer),
            )

    def get_report(self, job_id: str) -> str:
        """Get this customer's persisted background report and its actual status."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT status,result FROM jobs WHERE id=? AND customer=?", (job_id, self.customer)
            ).fetchone()
            return json.dumps(dict(row)) if row else "No report is available for this customer."

    def record_session(self, identifier: str, state: dict[str, object]) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (identifier, json.dumps(state)))
