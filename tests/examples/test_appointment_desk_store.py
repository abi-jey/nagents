"""SQLite appointment-desk invariants: approval, revisions, idempotence, ownership."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from typing import cast

from examples.live.appointment_desk.store import Desk

if TYPE_CHECKING:
    from pathlib import Path

SLOT = "2030-08-06 14:00 UTC"
OTHER_SLOT = "2030-08-07 14:00 UTC"


def desk(tmp_path: Path, customer: str = "demo-customer") -> Desk:
    return Desk(tmp_path / "desk.db", customer=customer)


def committed(result: str) -> dict[str, object]:
    value = cast("dict[str, object]", json.loads(result))
    assert value["status"] == "completed"
    return value


def test_approval_is_required_before_commit(tmp_path: Path) -> None:
    store = desk(tmp_path)
    assert json.loads(store.propose_booking(SLOT))["revision"] == 1
    assert store.commit(1) == "Not executed: current proposal needs explicit application approval."
    assert store.state()["bookings"] == []


def test_revision_change_clears_approval_and_stale_commit_fails(tmp_path: Path) -> None:
    store = desk(tmp_path)
    store.propose_booking(SLOT)
    store.approve(1)
    assert json.loads(store.propose_booking(OTHER_SLOT))["revision"] == 2  # Replaces and unapproves.
    assert store.commit(2) == "Not executed: current proposal needs explicit application approval."
    assert store.commit(1) == "Not executed: current proposal needs explicit application approval."
    assert store.state()["bookings"] == []


def test_correct_approved_booking_persists_and_is_idempotent(tmp_path: Path) -> None:
    store = desk(tmp_path)
    store.propose_booking(SLOT)
    store.approve(1)
    first = committed(store.commit(1))
    second = committed(store.commit(1))  # Repeated commit returns the stored operation result.
    assert first == second
    bookings = store.state()["bookings"]
    assert bookings == [{"id": first["booking_id"], "slot": SLOT}]


def test_unique_slot_collision_between_customers(tmp_path: Path) -> None:
    first = desk(tmp_path, "customer-a")
    first.propose_booking(SLOT)
    first.approve(1)
    committed(first.commit(1))

    second = desk(tmp_path, "customer-b")
    second.propose_booking(SLOT)
    second.approve(1)
    assert second.commit(1) == "Not executed: that slot is no longer available."
    assert first.state()["bookings"] and second.state()["bookings"] == []


def test_change_and_cancel_require_owned_existing_booking(tmp_path: Path) -> None:
    store = desk(tmp_path)
    try:
        store.propose_change("missing-booking", OTHER_SLOT)
    except ValueError as error:
        assert "unavailable for this customer" in str(error)
    else:  # pragma: no cover - explicit failure
        raise AssertionError("change of an unowned booking must fail")
    try:
        store.propose_cancellation("missing-booking")
    except ValueError as error:
        assert "unavailable for this customer" in str(error)
    else:  # pragma: no cover - explicit failure
        raise AssertionError("cancellation of an unowned booking must fail")

    store.propose_booking(SLOT)
    store.approve(1)
    booking_id = str(committed(store.commit(1))["booking_id"])

    store.propose_change(booking_id, OTHER_SLOT)
    assert store.commit(2) == "Not executed: current proposal needs explicit application approval."
    store.approve(2)
    assert committed(store.commit(2))["slot"] == OTHER_SLOT
    assert store.state()["bookings"] == [{"id": booking_id, "slot": OTHER_SLOT}]

    store.propose_cancellation(booking_id)
    store.approve(3)
    assert committed(store.commit(3))["action"] == "cancel"
    assert store.state()["bookings"] == []


def test_concurrent_commit_never_repeats_the_action(tmp_path: Path) -> None:
    store = desk(tmp_path)
    store.propose_booking(SLOT)
    store.approve(1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.commit(1), range(2)))
    bookings = cast("list[object]", store.state()["bookings"])
    assert len({committed(result)["booking_id"] for result in results}) == 1
    assert len(bookings) == 1
