"""Agent server — FastAPI app with chat, tools, and attachments."""

import importlib.util
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from nagents import Agent, DoneEvent, ErrorEvent, Provider, ProviderType, SessionManager
from nagents import TextChunkEvent, ToolCallEvent, ToolResultEvent

from .tools import _attachments
from .tools import _collect_pending
from .tools import _reset_pending
from .tools import BASE_TOOLS

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

# ── Logging setup ────────────────────────────────────────────────────────────
logger = logging.getLogger("nagents.server")
logger.setLevel(logging.DEBUG)

_handler = BufferHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)

_nagents_logger = logging.getLogger("nagents")
_nagents_logger.setLevel(logging.DEBUG)
_nagents_logger.addHandler(_handler)
_nagents_logger.propagate = False

# ── Config from env ──────────────────────────────────────────────────────────


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

# ── Agent singleton ──────────────────────────────────────────────────────────
_agent: Agent | None = None
_custom_tools: list = []


def _provider_type(value: str) -> ProviderType:
    mapping: dict[str, ProviderType] = {
        "openrouter": ProviderType.OPENROUTER,
        "openai": ProviderType.OPENAI_COMPATIBLE,
        "anthropic": ProviderType.ANTHROPIC,
        "gemini": ProviderType.GEMINI_NATIVE,
    }
    return mapping.get(value, ProviderType.OPENROUTER)


def _load_custom_tools(tools_dir: str) -> list:
    loaded: list = []
    if not tools_dir:
        return loaded

    dir_path = Path(tools_dir)
    if not dir_path.is_dir():
        logger.warning("Tools dir not found: %s", tools_dir)
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


def _load_all_tools() -> list:
    global _custom_tools
    _custom_tools = _load_custom_tools(TOOLS_DIR)
    return BASE_TOOLS + _custom_tools


def _get_agent() -> Agent:
    global _agent
    if _agent is not None:
        return _agent

    logger.info("Creating agent provider=%s model=%s base_url=%s", PROVIDER_TYPE, MODEL, BASE_URL)

    provider = Provider(
        provider_type=_provider_type(PROVIDER_TYPE),
        api_key=API_KEY,
        model=MODEL,
        base_url=BASE_URL,
    )

    session_manager = SessionManager(db_path=SESSIONS_DB)
    tools = _load_all_tools()

    _agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        streaming=True,
    )

    logger.info("Agent created with %d tools", len(tools))
    return _agent


def _rebuild_agent() -> Agent:
    global _agent
    old = _agent
    _agent = None
    try:
        return _get_agent()
    except Exception:
        _agent = old
        raise


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


# ── Routes ───────────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> ChatResponse:
    agent = _get_agent()
    _reset_pending()

    session_id = body.session_id or None
    logger.info("Chat user_id=%s session_id=%s message=%.100s", body.user_id, session_id, body.message)

    full_response: str = ""
    final_session_id: str | None = None

    try:
        async for event in agent.run(
            user_message=body.message,
            session_id=session_id,
            user_id=body.user_id,
        ):
            if isinstance(event, TextChunkEvent):
                full_response += event.chunk
            elif isinstance(event, ToolCallEvent):
                logger.info("Tool call: %s(%s)", event.name, event.arguments)
            elif isinstance(event, ToolResultEvent):
                logger.info("Tool result: %s -> %s", event.name,
                            str(event.result)[:100] if event.result else event.error)
            elif isinstance(event, DoneEvent):
                final_session_id = event.session_id
                logger.info("Done session=%s tokens=%s", event.session_id,
                            event.usage.total_tokens if event.usage else "?")
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
            attachments.append(AttachmentRef(
                id=a.id,
                filename=a.path.name,
                url=f"/attachments/{a.id}",
                description=a.description,
            ))

    return ChatResponse(
        response=full_response.strip() or "(no response)",
        session_id=final_session_id,
        attachments=attachments,
    )


@app.get("/logs")
async def logs(tail: int = 50) -> dict:
    tail = max(1, min(tail, len(LOG_BUFFER)))
    items = list(LOG_BUFFER)[-tail:]
    return {"count": len(items), "lines": items}


@app.get("/tools", response_model=list[ToolInfo])
async def list_tools() -> list[ToolInfo]:
    tools = _load_all_tools()
    return [
        ToolInfo(name=t.__name__, doc=(t.__doc__ or "").strip().split("\n\n")[0])
        for t in tools
    ]


@app.post("/tools/reload")
async def reload_tools() -> dict:
    try:
        _rebuild_agent()
        tools = _load_all_tools()
        return {"status": "ok", "count": len(tools), "names": [t.__name__ for t in tools]}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@app.get("/attachments", response_model=list[AttachmentInfo])
async def list_attachments() -> list[AttachmentInfo]:
    result = []
    for a in sorted(_attachments.values(), key=lambda x: x.created_at, reverse=True):
        size = a.path.stat().st_size if a.path.exists() else 0
        result.append(AttachmentInfo(
            id=a.id,
            filename=a.path.name,
            description=a.description,
            size=size,
            content_type=a.content_type,
            created_at=datetime.fromtimestamp(a.created_at, tz=timezone.utc).isoformat(),
            fetch_count=a.fetch_count,
            last_fetched_at=(
                datetime.fromtimestamp(a.last_fetched_at, tz=timezone.utc).isoformat()
                if a.last_fetched_at else None
            ),
        ))
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
