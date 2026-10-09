"""Exact opaque delegation lookup and bounded, separately expanded sent payloads."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.web.live_runtime import _Record
from tests.support.web import client_app
from tests.test_web_live_delegations import update

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("identifier", [".", "..", "provider:task/one.v2", "😀" * 256])
def test_exact_delegation_query_and_payload_inspection(tmp_path: Path, identifier: str) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            record = _Record()
            record.delegation(update(record, identifier, "queued"))
            record.delegation(update(record, identifier, "working", "run"))
            record.delegation(update(record, identifier, "completed", "run"))
            record.delegation(
                {
                    "voice_session_id": record.identifier,
                    "chat_session_id": "ngn-chat",
                    "delegation_id": identifier,
                    "live_append": {
                        "kind": "commentary",
                        "content": "🙂" * 600,
                        "wire_type": "session.commentary.append",
                    },
                }
            )
            app.state.live._records[record.identifier] = record
            url = f"/api/live/sessions/{record.identifier}/delegation-details"
            response = await client.get(url, params={"delegation_id": identifier}, headers=headers)
            assert response.status_code == 200
            details = response.json()
            sent = details["live_updates"][0]
            assert details["delegation_id"] == identifier and sent["seq"] == details["seq"]
            assert sent["outcome"] == "sent" and sent["content"]["truncated"]
            assert len(sent["content"]["text"].encode()) <= 2000
            assert details["timeline"][-1]["detail_type"] == "live_append"
            assert "🙂" not in str(record.snapshot(0))
            assert (await client.get(url, params={"delegation_id": identifier})).status_code == 403
            for query in (
                "delegation_id=x&delegation_id=y",
                "delegation_id=x&token=y",
                "delegation_id=%FF",
                "delegation_id=%20",
                "other=x",
            ):
                assert (await client.get(url + "?" + query, headers=headers)).status_code == 400

    asyncio.run(scenario())
