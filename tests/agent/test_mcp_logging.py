"""MCP diagnostics never echo protocol payloads or credential-bearing arguments."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from nagents.mcp import MCPServerConfig
from nagents.mcp.client import MCPClient


@pytest.mark.asyncio
async def test_tool_authorization_failure_is_classified_without_logging_response_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = MCPClient(MCPServerConfig("github", "unused"))
    content = [{"type": "text", "text": "Bad credentials: Authorization PRIVATE_BEARER_VALUE"}]
    client._send_request = AsyncMock(return_value={"isError": True, "content": content})  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        assert await client.call_tool("get_repository", {"argument": "PRIVATE_REQUEST_VALUE"}) == content
    assert "stage=tools/call" in caplog.text and "category=authorization" in caplog.text
    assert "PRIVATE_BEARER_VALUE" not in caplog.text and "PRIVATE_REQUEST_VALUE" not in caplog.text


@pytest.mark.asyncio
async def test_rpc_error_logs_stage_and_code_without_upstream_message(caplog: pytest.LogCaptureFixture) -> None:
    from nagents.mcp import MCPError
    from nagents.mcp.client import _PendingRequest

    client = MCPClient(MCPServerConfig("github", "unused"))
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"error":{"code":401,"message":"Unauthorized PRIVATE_TOKEN_VALUE"}}\n')
    reader.feed_eof()
    process = AsyncMock()
    process.stdout = reader
    client._process = process
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    client._pending[1] = _PendingRequest("tools/call", future)
    with caplog.at_level(logging.ERROR):
        await client._read_responses()
    with pytest.raises(MCPError):
        await future
    assert "stage=tools/call" in caplog.text and "code=401" in caplog.text
    assert "category=authorization" in caplog.text and "PRIVATE_TOKEN_VALUE" not in caplog.text
