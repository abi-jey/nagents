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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from nagents import Agent
from nagents import DoneEvent
from nagents import ErrorEvent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents import TextChunkEvent
from nagents import ToolCallEvent
from nagents import ToolResultEvent
from nagents.mcp import MCPManager
from nagents.mcp import MCPServerConfig

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

_handler = BufferHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)

_nagents_logger = logging.getLogger("nagents")
_nagents_logger.setLevel(logging.DEBUG)
_nagents_logger.addHandler(_handler)
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

PROVIDER_TYPE = _env("HAL_LLM_PROVIDER", "openrouter")
API_KEY = _env("HAL_LLM_API_KEY")
MODEL = _env("HAL_LLM_MODEL", "moonshotai/kimi-k2.6")
BASE_URL = _env("HAL_LLM_BASE_URL", "https://openrouter.ai/api/v1")
SYSTEM_PROMPT = _env(
    "HAL_SYSTEM_PROMPT",
    "You are a helpful AI assistant with access to shell, file, and directory tools. "
    "Use them when needed, and provide clear, concise responses.",
)
SESSIONS_DB = Path(_env("HAL_SESSIONS_DB", "/data/sessions.db"))
TOOLS_DIR = _env("HAL_TOOLS_DIR", "")
MCP_ENABLED = _env("HAL_MCP_ENABLED", "").lower() in ("1", "true", "yes")
MCP_CONFIG_PATH = _env("HAL_MCP_CONFIG", "")

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
    env_servers = _env("HAL_MCP_SERVERS", "")
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

    logger.info("Creating agent provider=%s model=%s", PROVIDER_TYPE, MODEL)

    provider = Provider(
        provider_type=_provider_type(PROVIDER_TYPE),
        api_key=API_KEY,
        model=MODEL,
        base_url=BASE_URL,
    )

    session_manager = SessionManager(db_path=SESSIONS_DB)
    tools: list[Callable[..., Any]] = list(BASE_TOOLS) + _custom_tools

    _agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
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
        provider = Provider(
            provider_type=_provider_type(PROVIDER_TYPE),
            api_key=API_KEY,
            model=MODEL,
            base_url=BASE_URL,
        )
        session_manager = SessionManager(db_path=SESSIONS_DB)

        _agent = Agent(
            provider=provider,
            session_manager=session_manager,
            tools=new_tools,
            system_prompt=SYSTEM_PROMPT,
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
            _mcp_config_hash = fp
            # Disconnect old, connect new
            if _mcp_manager:
                try:
                    await _mcp_manager.disconnect_all()
                except Exception:
                    logger.exception("MCP disconnect failed")
                _mcp_manager = None

            if configs:
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


# ── Routes ───────────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> ChatResponse:
    _reset_pending()

    # Auto-reload tools/MCP before processing
    sys_msg = await _reload_if_changed()
    user_message = body.message
    if sys_msg:
        user_message = f"{sys_msg}\n{body.message}"
        logger.info("System notification: %s", sys_msg)

    agent = _get_agent()

    session_id = body.session_id or None
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

    sys_msg = await _reload_if_changed()
    user_message = body.message
    if sys_msg:
        user_message = f"{sys_msg}\n{body.message}"

    agent = _get_agent()
    session_id = body.session_id or None
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
