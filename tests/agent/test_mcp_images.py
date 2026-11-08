"""MCP images are bounded native inputs, never base64 terminal output."""

import asyncio
import base64
from pathlib import Path

from nagents.mcp import MCPManager
from nagents.mcp import MCPServerConfig
from nagents.mcp.content import parse_tool_content
from nagents.tools.output import ToolOutput
from nagents.types import ToolCall

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()


def test_mcp_content_images_and_rejections() -> None:
    assert parse_tool_content([{"type": "text", "text": "hello"}]) == "hello"
    image = {"type": "image", "mimeType": "image/png", "data": PNG}
    result = parse_tool_content([{"type": "text", "text": "hello"}, image])
    assert isinstance(result, ToolOutput)
    assert result.images[0].base64_data == PNG
    assert PNG not in str(result)
    result = parse_tool_content([image] * 4)
    assert isinstance(result, ToolOutput)
    assert len(result.images) == 3
    assert "budget" in result.text
    for media, data in (("image/svg+xml", PNG), ("image/png", "invalid!"), ("image/png", "YWJj")):
        rejected = parse_tool_content([{"type": "image", "mimeType": media, "data": data}])
        assert isinstance(rejected, str)
        assert "omitted" in rejected
        assert data not in rejected


def test_real_mcp_stdio_image_round_trip(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        ' if "id" not in r:continue\n'
        ' method=r["method"]\n'
        ' if method=="initialize":result={"protocolVersion":"2025-06-18","capabilities":{},"serverInfo":{}}\n'
        ' elif method=="tools/list":result={"tools":[{"name":"capture","description":"Capture","inputSchema":{"type":"object","properties":{}}}]}\n'
        f' else:result={{"content":[{{"type":"image","mimeType":"image/png","data":"{PNG}"}}]}}\n'
        ' print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":result}),flush=True)\n'
    )

    async def drive() -> None:
        import sys

        manager = MCPManager([MCPServerConfig(name="fixture", command=sys.executable, args=[str(server)])])
        try:
            await manager.connect_all()
            result = await manager.execute_tool(ToolCall(id="capture", name="mcp__fixture__capture", arguments={}))
            assert isinstance(result, ToolOutput)
            assert result.images[0].base64_data == PNG
        finally:
            await manager.disconnect_all()

    asyncio.run(drive())
