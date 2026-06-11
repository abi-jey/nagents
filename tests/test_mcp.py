"""Tests for the nagents MCP integration."""

import asyncio
import inspect
from typing import Any
from typing import cast

from nagents.mcp import MCPError
from nagents.mcp import MCPManager
from nagents.mcp import MCPServerConfig
from nagents.mcp.client import MCP_PROTOCOL_VERSION
from nagents.mcp.client import _PendingRequest
from nagents.mcp.client import _extract_text_content
from nagents.mcp.manager import _MCPToolInfo
from nagents.mcp.manager import _mcp_type_to_python
from nagents.mcp.manager import _param_to_mcp_name
from nagents.tools.registry import ToolRegistry
from nagents.types import JsonSchema


class TestMCPServerConfig:
    def test_defaults(self) -> None:
        config = MCPServerConfig(name="test", command="python")
        assert config.name == "test"
        assert config.command == "python"
        assert config.args == []
        assert config.env is None
        assert config.cwd is None

    def test_full_args(self) -> None:
        config = MCPServerConfig(
            name="playwright",
            command="npx",
            args=["@playwright/mcp@latest"],
            env={"NODE_ENV": "test"},
            cwd="/tmp",
        )
        assert config.name == "playwright"
        assert config.command == "npx"
        assert config.args == ["@playwright/mcp@latest"]
        assert config.env == {"NODE_ENV": "test"}
        assert config.cwd == "/tmp"


class TestMCPError:
    def test_basic(self) -> None:
        err = MCPError("something went wrong")
        assert "something went wrong" in str(err)
        assert err.code == -1
        assert err.data is None

    def test_with_code_and_data(self) -> None:
        err = MCPError(message="tool not found", code=-32601, data={"name": "bad_tool"})
        assert err.code == -32601
        assert err.data == {"name": "bad_tool"}
        assert "tool not found" in str(err)
        assert "-32601" in str(err)


class TestExtractTextContent:
    def test_single_text_item(self) -> None:
        content = [{"type": "text", "text": "hello"}]
        assert _extract_text_content(content) == "hello"

    def test_multiple_text_items(self) -> None:
        content = [
            {"type": "text", "text": "line1"},
            {"type": "text", "text": "line2"},
        ]
        assert _extract_text_content(content) == "line1\nline2"

    def test_mixed_content_types(self) -> None:
        content = [
            {"type": "text", "text": "hello"},
            {"type": "image", "data": "base64..."},
            {"type": "text", "text": "world"},
            {"type": "resource_link", "uri": "file:///foo"},
        ]
        assert _extract_text_content(content) == "hello\nworld"

    def test_empty_list(self) -> None:
        assert _extract_text_content([]) == ""

    def test_no_text_items(self) -> None:
        content = [{"type": "image", "data": "base64..."}]
        assert _extract_text_content(content) == ""

    def test_empty_text_value(self) -> None:
        content = [{"type": "text", "text": ""}]
        assert _extract_text_content(content) == ""


class TestMCPToolInfo:
    def test_basic(self) -> None:
        info = _MCPToolInfo(
            server_name="playwright",
            tool_name="browser_navigate",
            description="Navigate to a URL",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        )
        assert info.server_name == "playwright"
        assert info.tool_name == "browser_navigate"
        assert info.description == "Navigate to a URL"
        assert info.input_schema["required"] == ["url"]

    def test_with_title(self) -> None:
        info = _MCPToolInfo(
            server_name="filesystem",
            tool_name="read_file",
            description="Read file contents",
            input_schema={},
            title="File Reader",
        )
        assert info.title == "File Reader"


class TestCreateToolWrapper:
    """Tests for _create_tool_wrapper signature generation."""

    def test_wrapper_has_proper_name(self) -> None:
        manager = MCPManager([])
        info = _MCPToolInfo(
            server_name="test",
            tool_name="my_tool",
            description="Does something",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
        )
        wrapper = manager._create_tool_wrapper("mcp__test__my_tool", info)
        assert wrapper.__name__ == "mcp__test__my_tool"
        assert wrapper.__doc__ == "Does something"

    def test_wrapper_signature_required_param(self) -> None:
        manager = MCPManager([])
        info = _MCPToolInfo(
            server_name="test",
            tool_name="navigate",
            description="Go somewhere",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        )
        wrapper = manager._create_tool_wrapper("mcp__test__navigate", info)
        sig = inspect.signature(wrapper)

        assert "url" in sig.parameters
        url_param = sig.parameters["url"]
        assert url_param.default is inspect.Parameter.empty  # required
        assert url_param.kind == inspect.Parameter.KEYWORD_ONLY

    def test_wrapper_signature_optional_param(self) -> None:
        manager = MCPManager([])
        info = _MCPToolInfo(
            server_name="test",
            tool_name="search",
            description="Search something",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        )
        wrapper = manager._create_tool_wrapper("mcp__test__search", info)
        sig = inspect.signature(wrapper)

        assert "limit" in sig.parameters
        assert sig.parameters["limit"].default is None  # optional
        assert "query" in sig.parameters
        assert sig.parameters["query"].default is inspect.Parameter.empty  # required

    def test_wrapper_annotations(self) -> None:
        manager = MCPManager([])
        info = _MCPToolInfo(
            server_name="test",
            tool_name="echo",
            description="Echo back",
            input_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                },
                "required": ["message"],
            },
        )
        wrapper = manager._create_tool_wrapper("mcp__test__echo", info)
        assert wrapper.__annotations__ == {"message": str}

    def test_wrapper_no_properties(self) -> None:
        """Wrapper with no inputSchema properties keeps **kwargs."""
        manager = MCPManager([])
        info = _MCPToolInfo(
            server_name="test",
            tool_name="no_args",
            description="No arguments needed",
            input_schema={"type": "object"},
        )
        wrapper = manager._create_tool_wrapper("mcp__test__no_args", info)
        sig = inspect.signature(wrapper)
        # Should have **kwargs only (no named parameters)
        has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        assert has_kwargs

    def test_wrapper_hyphen_to_underscore(self) -> None:
        """Hyphenated MCP param names become underscores in Python."""
        manager = MCPManager([])
        info = _MCPToolInfo(
            server_name="test",
            tool_name="do_thing",
            description="Does thing",
            input_schema={
                "type": "object",
                "properties": {
                    "page-url": {"type": "string"},
                },
                "required": ["page-url"],
            },
        )
        wrapper = manager._create_tool_wrapper("mcp__test__do_thing", info)
        sig = inspect.signature(wrapper)
        assert "page_url" in sig.parameters  # hyphen -> underscore

    def test_multiple_params_sorted(self) -> None:
        """Parameters should be sorted alphabetically."""
        manager = MCPManager([])
        info = _MCPToolInfo(
            server_name="test",
            tool_name="multi",
            description="Multiple params",
            input_schema={
                "type": "object",
                "properties": {
                    "z": {"type": "string"},
                    "a": {"type": "string"},
                    "m": {"type": "string"},
                },
                "required": [],
            },
        )
        wrapper = manager._create_tool_wrapper("mcp__test__multi", info)
        sig = inspect.signature(wrapper)
        param_names = list(sig.parameters.keys())
        assert param_names == ["a", "m", "z"]


class TestToolRegistryWithParameters:
    """Tests for ToolRegistry.register with explicit parameters."""

    def test_register_with_explicit_schema(self) -> None:
        async def my_tool(x: str) -> str:
            return x

        registry = ToolRegistry()
        td = registry.register(
            my_tool,
            name="custom_tool",
            description="Custom description",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "string", "description": "The X value"}},
                "required": ["x"],
            },
        )
        assert td.name == "custom_tool"
        assert td.description == "Custom description"
        assert td.parameters["properties"]["x"]["description"] == "The X value"
        assert td.parameters["required"] == ["x"]

    def test_register_without_explicit_schema_auto_extracts(self) -> None:
        async def my_tool(x: str, y: int = 5) -> str:
            """A tool function."""
            return f"{x}:{y}"

        registry = ToolRegistry()
        td = registry.register(my_tool)
        assert td.name == "my_tool"
        assert td.description == "A tool function."
        assert "x" in td.parameters["properties"]
        assert "y" in td.parameters["properties"]
        assert "x" in td.parameters["required"]
        assert "y" not in td.parameters["required"]


class TestMCPManagerToolMap:
    """Tests for MCPManager internal tool map management."""

    def test_empty_configs(self) -> None:
        manager = MCPManager([])
        assert not manager.is_connected
        assert manager._tool_map == {}
        assert manager._clients == {}

    def test_configs_stored(self) -> None:
        configs = [
            MCPServerConfig(name="a", command="python"),
            MCPServerConfig(name="b", command="node"),
        ]
        manager = MCPManager(configs)
        assert len(manager.configs) == 2
        assert manager.configs[0].name == "a"

    def test_get_tools_empty(self) -> None:
        """get_tools returns empty list when no tools discovered."""
        manager = MCPManager([])

        async def run() -> list[Any]:
            return await manager.get_tools()

        tools = asyncio.run(run())
        assert tools == []


class TestMCPProtocolConstants:
    def test_protocol_version(self) -> None:
        assert MCP_PROTOCOL_VERSION == "2025-06-18"

    def test_pending_request_dataclass(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            fut: asyncio.Future[dict[str, Any]] = loop.create_future()
            pr = _PendingRequest(method="tools/list", future=fut)
            assert pr.method == "tools/list"
            assert pr.future is fut
        finally:
            loop.close()


class TestToolRegistrationIntegration:
    """Integration test: wrapper → ToolRegistry → correct schema."""

    def test_wrapper_registers_with_correct_required(self) -> None:
        manager = MCPManager([])
        info = _MCPToolInfo(
            server_name="test",
            tool_name="create_user",
            description="Create a user",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "admin": {"type": "boolean"},
                },
                "required": ["name", "email"],
            },
        )
        wrapper = manager._create_tool_wrapper("mcp__test__create_user", info)

        registry = ToolRegistry()
        td = registry.register(wrapper)

        # Auto-extracted schema should mark required params correctly
        assert td.name == "mcp__test__create_user"
        assert "name" in td.parameters["required"]
        assert "email" in td.parameters["required"]
        assert "admin" not in td.parameters["required"]
        # name + email typed as string, admin as string (all str annotations)
        assert td.parameters["properties"]["name"]["type"] == "string"

    def test_wrapper_registers_with_explicit_mcp_schema(self) -> None:
        """Passing the MCP input schema preserves types and descriptions."""
        manager = MCPManager([])
        mcp_schema = {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name to get weather for",
                },
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                },
            },
            "required": ["city"],
        }
        info = _MCPToolInfo(
            server_name="weather",
            tool_name="get_forecast",
            description="Get weather forecast",
            input_schema=mcp_schema,
        )
        wrapper = manager._create_tool_wrapper("mcp__weather__get_forecast", info)

        registry = ToolRegistry()
        td = registry.register(wrapper, parameters=cast(JsonSchema, mcp_schema))

        # Schema should match exactly what MCP server provided
        assert td.parameters["properties"]["city"]["description"] == "City name to get weather for"
        assert td.parameters["properties"]["units"]["enum"] == ["metric", "imperial"]
        assert td.parameters["required"] == ["city"]


class TestMCPTypeMapping:
    """Tests for _mcp_type_to_python — MCP JSON schema → Python type."""

    def test_integer(self) -> None:
        assert _mcp_type_to_python({"type": "integer"}, "count") is int

    def test_number(self) -> None:
        assert _mcp_type_to_python({"type": "number"}, "price") is float

    def test_boolean(self) -> None:
        assert _mcp_type_to_python({"type": "boolean"}, "enabled") is bool

    def test_array(self) -> None:
        assert _mcp_type_to_python({"type": "array"}, "items") is list

    def test_object(self) -> None:
        assert _mcp_type_to_python({"type": "object"}, "config") is dict

    def test_string(self) -> None:
        assert _mcp_type_to_python({"type": "string"}, "name") is str

    def test_unknown_defaults_to_str(self) -> None:
        assert _mcp_type_to_python({"type": "custom"}, "x") is str

    def test_missing_type_defaults_to_str(self) -> None:
        assert _mcp_type_to_python({}, "x") is str


class TestMCPParamNameMapping:
    """Tests for _param_to_mcp_name — Python safe name → MCP original name."""

    def test_exact_match(self) -> None:
        props = {"url": {"type": "string"}}
        assert _param_to_mcp_name("url", props) == "url"

    def test_hyphen_to_underscore(self) -> None:
        props = {"page-url": {"type": "string"}}
        assert _param_to_mcp_name("page_url", props) == "page-url"

    def test_no_match_returns_safe_name(self) -> None:
        props = {"url": {"type": "string"}}
        assert _param_to_mcp_name("unknown", props) == "unknown"


class TestWrapperTypeAnnotations:
    """Tests that wrapper annotations match MCP schema types."""

    def test_integer_param_annotated_as_int(self) -> None:
        manager = MCPManager([])
        info = _MCPToolInfo(
            server_name="test",
            tool_name="resize",
            description="Resize viewport",
            input_schema={
                "type": "object",
                "properties": {
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "full_page": {"type": "boolean"},
                    "device_scale": {"type": "number"},
                },
                "required": ["width", "height"],
            },
        )
        wrapper = manager._create_tool_wrapper("mcp__test__resize", info)
        annots = wrapper.__annotations__

        assert annots["width"] is int
        assert annots["height"] is int
        assert annots["full_page"] is bool
        assert annots["device_scale"] is float

    def test_registry_extracts_correct_types(self) -> None:
        """ToolRegistry should produce integer/number schema from wrapper annotations."""
        manager = MCPManager([])
        info = _MCPToolInfo(
            server_name="test",
            tool_name="resize",
            description="Resize viewport",
            input_schema={
                "type": "object",
                "properties": {
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
                "required": ["width", "height"],
            },
        )
        wrapper = manager._create_tool_wrapper("mcp__test__resize", info)
        registry = ToolRegistry()
        td = registry.register(wrapper)

        assert td.parameters["properties"]["width"]["type"] == "integer"
        assert td.parameters["properties"]["height"]["type"] == "integer"
        assert td.parameters["required"] == ["height", "width"]  # sorted alphabetically
