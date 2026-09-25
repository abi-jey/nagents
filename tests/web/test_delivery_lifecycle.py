"""Local deliveries follow web trash membership and permanent purge."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.channels.delivery_types import DeliveryOrigin
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelFile
from nagents.channels.types import ChannelSend
from nagents.channels.types import ChannelSendCapabilities
from nagents.session.deliveries import DeliveryJournal
from nagents.types import Message
from nagents.types import ToolCall
from tests.support.web import client_app
from tests.web.test_web_deletion import quiet
from tests.web.test_web_deletion import rows

if TYPE_CHECKING:
    from nagents.web.service import WebState


@pytest.mark.parametrize("trash_purge", [False, True])
def test_delivery_trash_restore_purge(tmp_path: Path, trash_purge: bool) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            anchor = await state.history.add_message(
                root, Message(role="assistant", tool_calls=[ToolCall(id="send", name="channel_send", arguments={})])
            )
            journal = DeliveryJournal(state.history.db_path)
            origin = DeliveryOrigin(root, root, "run", "turn", "", 0, "invocation", anchor, 0, "send", "channel_send")
            message = ChannelSend(root, "saved", files=(ChannelFile("file.txt", "text/plain", b"asset"),))
            caps = ChannelSendCapabilities(text=True, file_media_types=("text/plain",))
            receipt = await journal.prepare(origin, "web", message, caps).commit()
            pending = journal.prepare(origin, "web", message, caps)
            response = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})
            assert response.status_code == 200, response.text
            item = response.json()["trash"]
            with pytest.raises(ChannelError):
                await journal.read_asset(root, receipt.delivery_id, receipt.assets[0].asset_id)
            with pytest.raises(ChannelError):
                await pending.commit()
            assert len(await rows(state, "SELECT delivery_id FROM ngn_local_deliveries")) == 1
            response = await client.post(
                f"/api/trash/{root}/restore", headers=headers, json={"deletion_id": item["deletion_id"]}
            )
            assert response.status_code == 200, response.text
            assert (await journal.history(root)).current == (receipt,)
            assert await journal.read_asset(root, receipt.delivery_id, receipt.assets[0].asset_id) == b"asset"
            if trash_purge:
                response = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})
                item = response.json()["trash"]
                response = await client.request(
                    "DELETE", f"/api/trash/{root}", headers=headers, json={"deletion_id": item["deletion_id"]}
                )
            else:
                response = await client.request(
                    "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True}
                )
            assert response.status_code == 200, response.text
            assert not await rows(state, "SELECT delivery_id FROM ngn_local_deliveries")
            assert not await rows(state, "SELECT asset_id FROM ngn_local_delivery_assets")

    asyncio.run(run())
