"""Agent server — FastAPI app with chat, tools, attachments, auto-reload, MCP."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import sys
import time
from collections import deque
from contextlib import suppress
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from collections.abc import Callable

from fastapi import FastAPI
from fastapi import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nagents import Agent
from nagents import DoneEvent
from nagents import ErrorEvent
from nagents import Provider
from nagents import ProviderType
from nagents import RateLimitEvent
from nagents import ReasoningChunkEvent
from nagents import SessionManager
from nagents import TextChunkEvent
from nagents import ToolCallEvent
from nagents import ToolResultEvent
from nagents.mcp import MCPManager
from nagents.mcp import MCPServerConfig

from .scheduler import set_session_context
from .scheduler import start_wakeup_loop
from .scheduler import stop_wakeup_loop
from .tools import BASE_TOOLS
from .tools import _attachments
from .tools import _collect_pending
from .tools import _reset_pending

# ── Log buffer ───────────────────────────────────────────────────────────────
LOG_BUFFER: deque[str] = deque(maxlen=1000)


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        LOG_BUFFER.append(self.format(record))


# ── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(title="Agent Server", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger("nagents.server")
logger.setLevel(logging.DEBUG)

_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_handler = BufferHandler()
_handler.setFormatter(_log_fmt)
logger.addHandler(_handler)

# Also log to stdout so logs appear in pod logs
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(_log_fmt)
logger.addHandler(_stdout_handler)

_nagents_logger = logging.getLogger("nagents")
_nagents_logger.setLevel(logging.DEBUG)
_nagents_logger.addHandler(_handler)
_nagents_logger.addHandler(_stdout_handler)
_nagents_logger.propagate = False

# ── Env config ───────────────────────────────────────────────────────────────


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")
    except ImportError:
        pass


_load_dotenv()

PROVIDER_TYPE = _env("NAGENTS_LLM_PROVIDER", "openrouter")
API_KEY = _env("NAGENTS_LLM_API_KEY")
MODEL = _env("NAGENTS_LLM_MODEL", "moonshotai/kimi-k2.6")
BASE_URL = _env("NAGENTS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
DEFAULT_SYSTEM_PROMPT = """\
You are a helpful AI assistant with access to tools for shell execution, file I/O, directory listing, \
file attachments, MCP server management, and MCP-provided capabilities (e.g., browser automation via Playwright).

## Environment

- The filesystem is read-only except for `/data` (persistent) and `/tmp` (ephemeral).
- Custom tools directory: `/data/tools/` — place `.py` files here, they auto-load on the next message.
- MCP config file: `/data/mcp.json` — JSON lines format, auto-reloads on the next message.

## Tool Usage Guidelines

### When to use tools
- Use `run_shell_command` for system operations: installing packages, running scripts, git commands, checking system state.
- Use `read_file` / `write_file` / `list_directory` for file operations (writes only work in /data and /tmp).
- Use `attach_file` to share generated output (screenshots, logs, documents) with the user via the UI.
- Use `add_mcp_server` to add new MCP servers at runtime (e.g., browser automation, filesystem access).
- Use MCP tools (prefixed with `mcp__`) for specialized capabilities like browser automation.

### Do's
- **Do** provide clear, concise arguments to tools.
- **Do** verify file paths exist before reading (use `list_directory` first if unsure).
- **Do** attach outputs the user would want to see (e.g., screenshots, generated files).
- **Do** chain tools when needed: list -> read -> modify -> write.
- **Do** keep shell commands simple and focused on one task.
- **Do** write custom tools to `/data/tools/` when you need new capabilities.
- **Do** use `add_mcp_server` when you need MCP-provided tools (e.g., Playwright for browser automation).
- **Do** use `wake_up_in` to schedule follow-ups (e.g., checking a long-running task, reminders).

### Don'ts
- **Don't** use tools for information you already know -- answer directly.
- **Don't** run destructive commands (rm -rf, drop tables) without confirming with the user.
- **Don't** write large files in a single `write_file` call -- split if >1000 lines.
- **Don't** ignore tool errors -- report them and suggest fixes.
- **Don't** try to write outside `/data` or `/tmp` -- the filesystem is read-only elsewhere.

## Writing Custom Tools

Custom tools are plain Python functions. Place `.py` files in `/data/tools/`; they are auto-loaded \
and become available immediately (no restart needed). Each callable function in the file becomes a tool.

### Example tool

```python
def get_weather(city: str) -> str:
    \"\"\"Get current weather for a city.

    Args:
        city: City name, e.g. "San Francisco"
    \"\"\"
    import urllib.request, json
    url = f"https://wttr.in/{city}?format=j1"
    resp = urllib.request.urlopen(url)
    data = json.loads(resp.read())
    current = data["current_condition"][0]
    return f"{city}: {current['temp_C']}C, {current['weatherDesc'][0]['value']}"
```

### Rules for custom tools
1. The function docstring is shown to the model -- make it descriptive and include parameter info.
2. Return a string (or JSON string for structured data).
3. Handle errors gracefully -- return error messages instead of raising exceptions.
4. Keep tools focused -- one tool, one job.
5. Type hints improve the model's understanding of arguments.
6. Functions starting with `_` are ignored (use for helpers).
7. Classes are ignored -- only plain functions become tools.

## Adding MCP Servers

Use the `add_mcp_server` tool to add MCP servers at runtime. Examples:

- Browser automation: `add_mcp_server(name="playwright", command="playwright-mcp", args="--browser chromium")`
- Filesystem access: `add_mcp_server(name="filesystem", command="npx", args="-y @modelcontextprotocol/server-filesystem /data")`

After adding an MCP server, its tools will be available on the next chat turn (prefixed with `mcp__<name>__`).
"""
SESSIONS_DB = Path(_env("NAGENTS_SESSIONS_DB", "/data/sessions.db"))
TOOLS_DIR = _env("NAGENTS_TOOLS_DIR", "/data/tools")
MCP_ENABLED = _env("NAGENTS_MCP_ENABLED", "").lower() in ("1", "true", "yes")
MCP_CONFIG_PATH = _env("NAGENTS_MCP_CONFIG", "/data/mcp.json")
CONFIGS_PATH = Path(_env("NAGENTS_CONFIGS_PATH", "/data/configs.json"))

# ── Ensure writable dirs exist ────────────────────────────────────────────────
for _d in [TOOLS_DIR, str(SESSIONS_DB.parent), str(CONFIGS_PATH.parent)]:
    with suppress(OSError):
        Path(_d).mkdir(parents=True, exist_ok=True)
        logger.info("Ensured directory exists: %s", _d)

logger.info(
    "Config: TOOLS_DIR=%s MCP_ENABLED=%s MCP_CONFIG=%s CONFIGS=%s SESSIONS_DB=%s",
    TOOLS_DIR,
    MCP_ENABLED,
    MCP_CONFIG_PATH,
    CONFIGS_PATH,
    SESSIONS_DB,
)

# ── Config management ─────────────────────────────────────────────────────────


def _embedded_config() -> dict[str, Any]:
    """Build the embedded default config from env vars."""
    return {
        "id": "default",
        "name": "Default (env)",
        "system_prompt": _env("NAGENTS_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        "model": MODEL,
        "provider": PROVIDER_TYPE,
        "base_url": BASE_URL,
        "api_key": "",
        "mcp_servers": [],
        "embedded": True,
        "created_at": "",
    }


def _load_all_configs() -> dict[str, dict[str, Any]]:
    """Load all configs: embedded default + saved user configs."""
    configs: dict[str, dict[str, Any]] = {"default": _embedded_config()}
    try:
        if CONFIGS_PATH.is_file():
            data = json.loads(CONFIGS_PATH.read_text())
            for cfg in data.get("configs", []):
                configs[cfg["id"]] = cfg
    except Exception:
        logger.exception("Failed to load configs from %s", CONFIGS_PATH)
    return configs


def _save_user_configs() -> None:
    """Save non-embedded configs to disk."""
    user_configs = [c for c in _all_configs.values() if not c.get("embedded")]
    try:
        CONFIGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIGS_PATH.write_text(json.dumps({"configs": user_configs}, indent=2))
    except Exception:
        logger.exception("Failed to save configs to %s", CONFIGS_PATH)


_all_configs: dict[str, dict[str, Any]] = _load_all_configs()
_active_config_id: str = "default"


def _active_config() -> dict[str, Any]:
    return _all_configs.get(_active_config_id) or _all_configs["default"]


# ── Tool reload tracking ─────────────────────────────────────────────────────
_tool_hashes: dict[int, str] = {}  # inode -> content hash (custom tools)
_mcp_config_hash: str = ""
_last_tool_names: list[str] = []
_last_mcp_tool_names: list[str] = []


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scan_custom_tool_files() -> dict[int, str]:
    """Return {inode: sha256} for all .py files in TOOLS_DIR."""
    hashes: dict[int, str] = {}
    if not TOOLS_DIR or not Path(TOOLS_DIR).is_dir():
        return hashes
    for py_file in sorted(Path(TOOLS_DIR).glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        with suppress(OSError):
            hashes[py_file.stat().st_ino] = _hash_file(py_file)
    return hashes


def _load_custom_tools(tools_dir: str) -> list[Callable[..., Any]]:
    loaded: list[Callable[..., Any]] = []
    if not tools_dir:
        return loaded

    dir_path = Path(tools_dir)
    if not dir_path.is_dir():
        return loaded

    for py_file in sorted(dir_path.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[py_file.stem] = mod
            spec.loader.exec_module(mod)
            for name in dir(mod):
                if name.startswith("_"):
                    continue
                obj = getattr(mod, name)
                if callable(obj) and not isinstance(obj, type):
                    loaded.append(obj)
                    logger.info("Loaded tool: %s from %s", name, py_file.name)
        except Exception:
            logger.exception("Failed to load tools from %s", py_file.name)

    return loaded


# ── MCP ──────────────────────────────────────────────────────────────────────


def _load_mcp_configs() -> list[MCPServerConfig]:
    """Load MCP server configs from file or env."""
    configs: list[MCPServerConfig] = []
    if not MCP_ENABLED:
        return configs

    # From config file (JSON lines: {name, command, args})
    if MCP_CONFIG_PATH and Path(MCP_CONFIG_PATH).is_file():
        import json

        try:
            raw = Path(MCP_CONFIG_PATH).read_text().strip()
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                entry = json.loads(line)
                configs.append(
                    MCPServerConfig(
                        name=entry["name"],
                        command=entry["command"],
                        args=entry.get("args", []),
                    )
                )
        except Exception:
            logger.exception("Failed to parse MCP config: %s", MCP_CONFIG_PATH)

    # From env var (comma-separated name=command patterns)
    env_servers = _env("NAGENTS_MCP_SERVERS", "")
    if env_servers:
        for spec in env_servers.split(","):
            spec = spec.strip()
            if not spec or "=" not in spec:
                continue
            name, cmd = spec.split("=", 1)
            parts = cmd.split()
            configs.append(
                MCPServerConfig(
                    name=name.strip(),
                    command=parts[0],
                    args=parts[1:] if len(parts) > 1 else [],
                )
            )

    return configs


def _mcp_config_fingerprint(configs: list[MCPServerConfig]) -> str:
    raw = "|".join(f"{c.name}:{c.command}:{','.join(c.args)}" for c in sorted(configs, key=lambda c: c.name))
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Agent singleton ──────────────────────────────────────────────────────────
_agent: Agent | None = None
_mcp_manager: MCPManager | None = None
_custom_tools: list[Callable[..., Any]] = []


def _provider_type(value: str) -> ProviderType:
    mapping: dict[str, ProviderType] = {
        "openrouter": ProviderType.OPENROUTER,
        "openai": ProviderType.OPENAI_COMPATIBLE,
        "anthropic": ProviderType.ANTHROPIC,
        "gemini": ProviderType.GEMINI_NATIVE,
    }
    return mapping.get(value, ProviderType.OPENROUTER)


def _get_agent() -> Agent:
    global _agent
    if _agent is not None:
        return _agent

    cfg = _active_config()
    model = cfg["model"]
    provider_type = cfg["provider"]
    api_key = cfg["api_key"] or API_KEY
    base_url = cfg["base_url"]
    sys_prompt = cfg["system_prompt"]

    logger.info("Creating agent config=%s provider=%s model=%s", cfg["name"], provider_type, model)

    provider = Provider(
        provider_type=_provider_type(provider_type),
        api_key=api_key,
        model=model,
        base_url=base_url,
    )

    session_manager = SessionManager(db_path=SESSIONS_DB)
    tools: list[Callable[..., Any]] = list(BASE_TOOLS) + _custom_tools

    _agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=tools,
        system_prompt=sys_prompt,
        streaming=True,
    )
    _agent._tools_list = tools  # type: ignore[attr-defined]
    _agent._mcp_manager = _mcp_manager  # type: ignore[attr-defined]

    logger.info("Agent created with %d tools", len(tools))
    return _agent


def _rebuild_agent(new_tools: list[Callable[..., Any]]) -> None:
    global _agent, _mcp_manager
    old_agent = _agent
    old_mcp = _mcp_manager
    _agent = None
    _mcp_manager = None
    try:
        cfg = _active_config()
        provider = Provider(
            provider_type=_provider_type(cfg["provider"]),
            api_key=cfg["api_key"] or API_KEY,
            model=cfg["model"],
            base_url=cfg["base_url"],
        )
        session_manager = SessionManager(db_path=SESSIONS_DB)

        _agent = Agent(
            provider=provider,
            session_manager=session_manager,
            tools=new_tools,
            system_prompt=cfg["system_prompt"],
            streaming=True,
        )
        _agent._tools_list = new_tools  # type: ignore[attr-defined]
        _agent._mcp_manager = old_mcp  # type: ignore[attr-defined]
        _mcp_manager = old_mcp

        logger.info("Agent rebuilt with %d tools", len(new_tools))
    except Exception:
        _agent = old_agent
        _mcp_manager = old_mcp
        raise


# ── Auto-reload logic ────────────────────────────────────────────────────────


def _collect_tool_names(tools: list[Callable[..., Any]]) -> list[str]:
    return sorted(t.__name__ for t in tools)


async def _reload_if_changed() -> str:
    """Check tool/MCP changes and rebuild agent if needed. Returns system notification or ''."""
    global _tool_hashes, _mcp_config_hash, _last_tool_names, _last_mcp_tool_names
    global _mcp_manager, _custom_tools
    messages: list[str] = []

    # ── Custom tools check ───────────────────────────────────────────────
    changed = False
    current_hashes = _scan_custom_tool_files()

    if current_hashes != _tool_hashes:
        changed = True
        added = set(current_hashes) - set(_tool_hashes)
        removed = set(_tool_hashes) - set(current_hashes)
        modified = set()
        for ino in set(current_hashes) & set(_tool_hashes):
            if current_hashes[ino] != _tool_hashes[ino]:
                modified.add(ino)

        logger.info(
            "Custom tools changed: +%d -%d ~%d (total=%d)",
            len(added),
            len(removed),
            len(modified),
            len(current_hashes),
        )
        if added or removed or modified:
            _custom_tools = _load_custom_tools(TOOLS_DIR)
            _tool_hashes = current_hashes
            new_names = _collect_tool_names(_custom_tools)
            old_names = list(_last_tool_names)
            _last_tool_names = new_names
            added_names = set(new_names) - set(old_names)
            removed_names = set(old_names) - set(new_names)
            if added_names:
                messages.append(f"Tools added: {', '.join(sorted(added_names))}")
            if removed_names:
                messages.append(f"Tools removed: {', '.join(sorted(removed_names))}")

    # ── MCP check ───────────────────────────────────────────────────────
    if MCP_ENABLED:
        configs = _load_mcp_configs()
        fp = _mcp_config_fingerprint(configs)
        if fp != _mcp_config_hash or (configs and _mcp_manager is None):
            logger.info("MCP config changed: %d servers configured (fingerprint mismatch or new)", len(configs))
            _mcp_config_hash = fp
            # Disconnect old, connect new
            if _mcp_manager:
                try:
                    await _mcp_manager.disconnect_all()
                    logger.info("MCP disconnected for reload")
                except Exception:
                    logger.exception("MCP disconnect failed")
                _mcp_manager = None

            if configs:
                logger.info("Connecting MCP servers: %s", [c.name for c in configs])
                try:
                    _mcp_manager = MCPManager(configs)
                    await _mcp_manager.connect_all()
                    mcp_tools: list[Any] = await _mcp_manager.get_tools()
                    mcp_names = sorted(getattr(t, "__name__", str(t)) for t in mcp_tools)
                    new_mcp = set(mcp_names) - set(_last_mcp_tool_names)
                    removed_mcp = set(_last_mcp_tool_names) - set(mcp_names)
                    _last_mcp_tool_names = mcp_names
                    if new_mcp:
                        messages.append(f"MCP tools added: {', '.join(sorted(new_mcp))}")
                    if removed_mcp:
                        messages.append(f"MCP tools removed: {', '.join(sorted(removed_mcp))}")
                    logger.info("MCP connected with %d tools", len(mcp_tools))
                except Exception:
                    logger.exception("MCP connect failed")
                    messages.append("MCP connection failed, tools unavailable")
            changed = True

    # ── Rebuild if anything changed ─────────────────────────────────────
    if changed:
        mcp_tool_defs: list[Any] = await _mcp_manager.get_tools() if _mcp_manager else []
        all_tools: list[Any] = list(BASE_TOOLS) + _custom_tools + mcp_tool_defs
        _rebuild_agent(all_tools)

    if messages:
        return "#system-notification: " + " | ".join(messages)

    return ""


# ── Initial tool scan ────────────────────────────────────────────────────────


def _init_tools() -> None:
    global _tool_hashes, _custom_tools, _last_tool_names
    _tool_hashes = _scan_custom_tool_files()
    _custom_tools = _load_custom_tools(TOOLS_DIR)
    _last_tool_names = _collect_tool_names(_custom_tools)


_init_tools()


# ── Models ───────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str
    session_id: str = ""
    user_id: str = "default"


class AttachmentRef(BaseModel):
    id: str
    filename: str
    url: str
    description: str


class ChatResponse(BaseModel):
    response: str
    session_id: str | None = None
    attachments: list[AttachmentRef] = []


class HealthResponse(BaseModel):
    status: str


class ToolInfo(BaseModel):
    name: str
    doc: str


class AttachmentInfo(BaseModel):
    id: str
    filename: str
    description: str
    size: int
    content_type: str
    created_at: str
    fetch_count: int
    last_fetched_at: str | None = None


class ToolsReloadResult(BaseModel):
    status: str
    count: int = 0
    names: list[str] = []
    detail: str = ""


class MCPServerSpec(BaseModel):
    name: str
    command: str
    args: list[str] = []


class AgentConfigCreate(BaseModel):
    name: str
    system_prompt: str = ""
    model: str = ""
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    mcp_servers: list[MCPServerSpec] = []


class AgentConfigOut(BaseModel):
    id: str
    name: str
    system_prompt: str
    model: str
    provider: str
    base_url: str
    mcp_servers: list[MCPServerSpec] = []
    embedded: bool = False
    created_at: str = ""


class ActiveConfigOut(BaseModel):
    active_id: str
    config: AgentConfigOut


class MCPToolInfo(BaseModel):
    name: str
    server: str
    description: str


class MCPServerStatus(BaseModel):
    name: str
    command: str
    args: list[str]
    connected: bool
    tools: list[MCPToolInfo] = []


# ── Routes ───────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def _startup() -> None:
    start_wakeup_loop()
    logger.info("Server startup complete")


@app.on_event("shutdown")
async def _shutdown() -> None:
    stop_wakeup_loop()
    logger.info("Server shutdown complete")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> ChatResponse:
    _reset_pending()
    session_id = body.session_id or None
    set_session_context(session_id, body.user_id)

    # Auto-reload tools/MCP before processing
    sys_msg = await _reload_if_changed()
    user_message = body.message
    if sys_msg:
        user_message = f"{sys_msg}\n{body.message}"
        logger.info("System notification: %s", sys_msg)

    agent = _get_agent()

    logger.info("Chat user_id=%s session_id=%s message=%.100s", body.user_id, session_id, body.message)

    full_response: str = ""
    final_session_id: str | None = None

    try:
        async for event in agent.run(
            user_message=user_message,
            session_id=session_id,
            user_id=body.user_id,
        ):
            if isinstance(event, TextChunkEvent):
                full_response += event.chunk
            elif isinstance(event, ToolCallEvent):
                logger.info("Tool call: %s(%s)", event.name, event.arguments)
            elif isinstance(event, ToolResultEvent):
                logger.info(
                    "Tool result: %s -> %s", event.name, str(event.result)[:100] if event.result else event.error
                )
            elif isinstance(event, DoneEvent):
                final_session_id = event.session_id
                logger.info(
                    "Done session=%s tokens=%s", event.session_id, event.usage.total_tokens if event.usage else "?"
                )
            elif isinstance(event, ErrorEvent):
                logger.error("Agent error: %s (recoverable=%s)", event.message, event.recoverable)
                if not event.recoverable:
                    if full_response:
                        break
                    raise RuntimeError(event.message)
    except Exception as exc:
        logger.exception("Agent run failed")
        return ChatResponse(response=f"Error: {exc}", session_id=final_session_id)

    attachments: list[AttachmentRef] = []
    for aid in _collect_pending():
        a = _attachments.get(aid)
        if a is not None:
            attachments.append(
                AttachmentRef(
                    id=a.id,
                    filename=a.path.name,
                    url=f"/attachments/{a.id}",
                    description=a.description,
                )
            )

    return ChatResponse(
        response=full_response.strip() or "(no response)",
        session_id=final_session_id,
        attachments=attachments,
    )


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest) -> StreamingResponse:
    _reset_pending()
    session_id = body.session_id or None
    set_session_context(session_id, body.user_id)

    sys_msg = await _reload_if_changed()
    user_message = body.message
    if sys_msg:
        user_message = f"{sys_msg}\n{body.message}"

    agent = _get_agent()
    logger.info("Chat SSE user_id=%s session_id=%s message=%.100s", body.user_id, session_id, body.message)

    async def event_stream() -> Any:
        final_session_id: str | None = None
        try:
            async for event in agent.run(
                user_message=user_message,
                session_id=session_id,
                user_id=body.user_id,
            ):
                if isinstance(event, TextChunkEvent):
                    yield f"data: {json.dumps({'type': 'text', 'content': event.chunk})}\n\n"
                elif isinstance(event, ReasoningChunkEvent):
                    yield f"data: {json.dumps({'type': 'reasoning', 'content': event.chunk})}\n\n"
                elif isinstance(event, ToolCallEvent):
                    yield f"data: {json.dumps({'type': 'tool_call', 'name': event.name, 'arguments': event.arguments})}\n\n"
                elif isinstance(event, ToolResultEvent):
                    result = event.result if event.result else event.error
                    yield f"data: {json.dumps({'type': 'tool_result', 'name': event.name, 'result': str(result)[:200]})}\n\n"
                elif isinstance(event, DoneEvent):
                    final_session_id = event.session_id
                    attachments_ref: list[dict[str, str]] = []
                    for aid in _collect_pending():
                        a = _attachments.get(aid)
                        if a is not None:
                            attachments_ref.append({"id": a.id, "filename": a.path.name, "url": f"/attachments/{a.id}"})
                    yield f"data: {json.dumps({'type': 'done', 'session_id': final_session_id, 'attachments': attachments_ref, 'tokens': event.usage.total_tokens if event.usage else 0})}\n\n"
                elif isinstance(event, ErrorEvent):
                    yield f"data: {json.dumps({'type': 'error', 'message': event.message, 'recoverable': event.recoverable})}\n\n"
                    if not event.recoverable:
                        break
                elif isinstance(event, RateLimitEvent):
                    yield f"data: {json.dumps({'type': 'rate_limit', 'attempt': event.attempt, 'max_retries': event.max_retries, 'retry_after': event.retry_after, 'status_code': event.status_code})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/logs")
async def logs(tail: int = 50) -> dict[str, object]:
    tail = max(1, min(tail, len(LOG_BUFFER)))
    items = list(LOG_BUFFER)[-tail:]
    return {"count": len(items), "lines": items}


# ── Tools API ────────────────────────────────────────────────────────────────


@app.get("/tools", response_model=list[ToolInfo])
async def list_tools() -> list[ToolInfo]:
    tools: list[Any] = list(BASE_TOOLS) + _custom_tools
    if _mcp_manager:
        tools.extend(await _mcp_manager.get_tools())
    return [
        ToolInfo(name=getattr(t, "__name__", str(t)), doc=(getattr(t, "__doc__", None) or "").strip().split("\n\n")[0])
        for t in tools
    ]


@app.post("/tools/reload", response_model=ToolsReloadResult)
async def reload_tools() -> ToolsReloadResult:
    try:
        global _tool_hashes
        _tool_hashes = {}  # force rescan
        await _reload_if_changed()
        tools = list(BASE_TOOLS) + _custom_tools
        if _mcp_manager:
            tools.extend(await _mcp_manager.get_tools())
        return ToolsReloadResult(status="ok", count=len(tools), names=_collect_tool_names(tools))
    except Exception as exc:
        return ToolsReloadResult(status="error", detail=str(exc))


# ── Config API ────────────────────────────────────────────────────────────────


def _config_to_out(cfg: dict[str, Any]) -> AgentConfigOut:
    return AgentConfigOut(
        id=cfg["id"],
        name=cfg["name"],
        system_prompt=cfg.get("system_prompt", ""),
        model=cfg.get("model", ""),
        provider=cfg.get("provider", ""),
        base_url=cfg.get("base_url", ""),
        mcp_servers=[MCPServerSpec(**s) for s in cfg.get("mcp_servers", [])],
        embedded=cfg.get("embedded", False),
        created_at=cfg.get("created_at", ""),
    )


@app.get("/configs", response_model=list[AgentConfigOut])
async def list_configs() -> list[AgentConfigOut]:
    return [_config_to_out(c) for c in _all_configs.values()]


@app.get("/configs/active", response_model=ActiveConfigOut)
async def get_active_config() -> ActiveConfigOut:
    return ActiveConfigOut(active_id=_active_config_id, config=_config_to_out(_active_config()))


@app.post("/configs", response_model=AgentConfigOut)
async def create_config(body: AgentConfigCreate) -> AgentConfigOut:
    import uuid

    cfg_id = uuid.uuid4().hex[:12]
    cfg: dict[str, Any] = {
        "id": cfg_id,
        "name": body.name,
        "system_prompt": body.system_prompt or _active_config()["system_prompt"],
        "model": body.model or _active_config()["model"],
        "provider": body.provider or _active_config()["provider"],
        "base_url": body.base_url or _active_config()["base_url"],
        "api_key": body.api_key,
        "mcp_servers": [s.model_dump() for s in body.mcp_servers],
        "embedded": False,
        "created_at": datetime.now(tz=UTC).isoformat(),
    }
    _all_configs[cfg_id] = cfg
    _save_user_configs()
    logger.info(
        "Created config: %s (id=%s, model=%s, provider=%s, mcp=%d)",
        cfg["name"],
        cfg_id,
        cfg["model"],
        cfg["provider"],
        len(cfg["mcp_servers"]),
    )
    return _config_to_out(cfg)


@app.put("/configs/{cfg_id}", response_model=AgentConfigOut)
async def update_config(cfg_id: str, body: AgentConfigCreate) -> AgentConfigOut:
    if cfg_id not in _all_configs:
        return Response(content='{"detail":"not found"}', status_code=404, media_type="application/json")
    cfg = _all_configs[cfg_id]
    if cfg.get("embedded"):
        return Response(
            content='{"detail":"cannot edit embedded config"}', status_code=403, media_type="application/json"
        )
    cfg["name"] = body.name
    cfg["system_prompt"] = body.system_prompt or cfg.get("system_prompt", "")
    cfg["model"] = body.model or cfg.get("model", "")
    cfg["provider"] = body.provider or cfg.get("provider", "")
    cfg["base_url"] = body.base_url or cfg.get("base_url", "")
    if body.api_key:
        cfg["api_key"] = body.api_key
    cfg["mcp_servers"] = [s.model_dump() for s in body.mcp_servers]
    _save_user_configs()
    logger.info("Updated config: %s (id=%s)", cfg["name"], cfg_id)
    if cfg_id == _active_config_id:
        await _activate_config_int(cfg_id)
    return _config_to_out(cfg)


@app.delete("/configs/{cfg_id}")
async def delete_config(cfg_id: str) -> dict[str, str]:
    if cfg_id not in _all_configs:
        return Response(content='{"detail":"not found"}', status_code=404, media_type="application/json")
    cfg = _all_configs[cfg_id]
    if cfg.get("embedded"):
        return Response(
            content='{"detail":"cannot delete embedded config"}', status_code=403, media_type="application/json"
        )
    del _all_configs[cfg_id]
    _save_user_configs()
    logger.info("Deleted config: %s (id=%s)", cfg.get("name", "?"), cfg_id)
    if _active_config_id == cfg_id:
        await _activate_config_int("default")
    return {"status": "deleted", "id": cfg_id}


async def _activate_config_int(cfg_id: str) -> None:
    """Internal: switch active config and rebuild agent."""
    global _active_config_id, _agent, _mcp_manager
    if cfg_id not in _all_configs:
        raise ValueError(f"Config not found: {cfg_id}")
    _active_config_id = cfg_id
    cfg = _active_config()
    logger.info(
        "Activating config: %s (id=%s, model=%s, provider=%s)", cfg["name"], cfg_id, cfg["model"], cfg["provider"]
    )

    # Disconnect old MCP if present
    if _mcp_manager:
        with suppress(Exception):
            await _mcp_manager.disconnect_all()
        _mcp_manager = None
        logger.info("Disconnected previous MCP manager")

    # Connect MCP servers from config (or fall back to env)
    cfg_mcp = cfg.get("mcp_servers", [])
    if cfg_mcp:
        configs = [MCPServerConfig(name=s["name"], command=s["command"], args=s.get("args", [])) for s in cfg_mcp]
        logger.info("Connecting %d MCP servers from config: %s", len(configs), [c.name for c in configs])
        try:
            _mcp_manager = MCPManager(configs)
            await _mcp_manager.connect_all()
            mcp_tools = await _mcp_manager.get_tools()
            logger.info("MCP connected: %d tools from %d servers", len(mcp_tools), len(configs))
        except Exception:
            logger.exception("MCP connect failed for config %s", cfg["name"])
            _mcp_manager = None
    elif MCP_ENABLED:
        logger.info("No MCP in config, falling back to env-based MCP")
        await _reload_if_changed()

    # Rebuild agent with new config settings + tools
    mcp_tools: list[Any] = await _mcp_manager.get_tools() if _mcp_manager else []
    all_tools: list[Any] = list(BASE_TOOLS) + _custom_tools + mcp_tools
    _rebuild_agent(all_tools)
    logger.info("Activated config: %s (id=%s, total tools=%d)", cfg["name"], cfg_id, len(all_tools))


@app.post("/configs/{cfg_id}/activate", response_model=ActiveConfigOut)
async def activate_config(cfg_id: str) -> ActiveConfigOut:
    if cfg_id not in _all_configs:
        return Response(content='{"detail":"not found"}', status_code=404, media_type="application/json")
    await _activate_config_int(cfg_id)
    return ActiveConfigOut(active_id=_active_config_id, config=_config_to_out(_active_config()))


# ── MCP Status API ────────────────────────────────────────────────────────────


@app.get("/mcp/status", response_model=list[MCPServerStatus])
async def mcp_status() -> list[MCPServerStatus]:
    if not _mcp_manager:
        return []
    result: list[MCPServerStatus] = []
    for cfg in _mcp_manager.configs:
        client = _mcp_manager._clients.get(cfg.name)
        connected = client.is_connected if client else False
        tools: list[MCPToolInfo] = []
        for _, info in _mcp_manager._tool_map.items():
            if info.server_name == cfg.name:
                tools.append(MCPToolInfo(name=info.tool_name, server=cfg.name, description=info.description))
        result.append(
            MCPServerStatus(name=cfg.name, command=cfg.command, args=list(cfg.args), connected=connected, tools=tools)
        )
    return result


# ── Attachments API ──────────────────────────────────────────────────────────


@app.get("/attachments", response_model=list[AttachmentInfo])
async def list_attachments() -> list[AttachmentInfo]:
    result: list[AttachmentInfo] = []
    for a in sorted(_attachments.values(), key=lambda x: x.created_at, reverse=True):
        size = a.path.stat().st_size if a.path.exists() else 0
        result.append(
            AttachmentInfo(
                id=a.id,
                filename=a.path.name,
                description=a.description,
                size=size,
                content_type=a.content_type,
                created_at=datetime.fromtimestamp(a.created_at, tz=UTC).isoformat(),
                fetch_count=a.fetch_count,
                last_fetched_at=(
                    datetime.fromtimestamp(a.last_fetched_at, tz=UTC).isoformat() if a.last_fetched_at else None
                ),
            )
        )
    return result


@app.get("/attachments/{attachment_id}")
async def get_attachment(attachment_id: str) -> Response:
    a = _attachments.get(attachment_id)
    if a is None or not a.path.exists():
        return Response(content=b"not found", status_code=404)

    a.fetch_count += 1
    a.last_fetched_at = time.time()

    return Response(
        content=a.path.read_bytes(),
        media_type=a.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{a.path.name}"',
            "X-Attachment-Id": a.id,
            "X-Fetch-Count": str(a.fetch_count),
        },
    )


# ── Web UI ───────────────────────────────────────────────────────────────────
_WEBUI_DIR = Path(__file__).resolve().parent / "webui"

if _WEBUI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_WEBUI_DIR), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        index_file = _WEBUI_DIR / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return Response(content=b"web ui not found", status_code=404)
