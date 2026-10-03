"""Bounded text-only startup context, separate from instructions and caller speech."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import Literal

from nagents.channels.runtime import _INBOUND_PREFIX

from ._async import finish_on_cancel
from .routing import RoutingStore

if TYPE_CHECKING:
    from pathlib import Path

Payload = dict[str, object]
ContextMode = Literal["recent", "summary", "none"]
MAX_SEED_BYTES = 7000
MAX_SUMMARY_BYTES = 2000
MAX_MESSAGE_BYTES = 1600
MAX_SOURCE_BYTES = 96_000
MAX_RECENT_MESSAGES = 12
MAX_SOURCE_MESSAGES = 128
MAX_SOURCE_CAPTIONS = 512
_EXCERPT = "\n[Remaining text omitted.]"


def bounded_text(text: str, budget: int) -> str:
    """Clip UTF-8 without splitting a code point; bytes also bound token input."""
    return text.encode("utf-8")[: max(0, budget)].decode("utf-8", errors="ignore")


def _text(item: Payload) -> str:
    content = item.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return ""
    value = content[0].get("text")
    return value if isinstance(value, str) else ""


def _message(role: str, text: str) -> Payload:
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}],
    }


@dataclass(frozen=True)
class LiveSeed:
    history: tuple[Payload, ...] = ()
    summary_source: str = field(default="", repr=False)
    recent: tuple[Payload, ...] = field(default=(), repr=False)
    mode: ContextMode = "none"
    method: str = "none"
    chat_session_id: str = ""
    fingerprint: str = ""
    source_messages: int = 0
    omitted_messages: int = 0
    omitted_content: bool = False
    omitted_caption_fragments: int = 0
    summary_included: bool = False
    notice: str = ""
    task_state: str = ""

    def report(self) -> Payload:
        """Only bounded metadata may enter HTTP responses and polling snapshots."""
        text = "".join(_text(item) for item in self.history)
        return {
            "mode": self.mode,
            "method": self.method,
            "chat_session_id": self.chat_session_id,
            "fingerprint": self.fingerprint,
            "message_count": len(self.history),
            "characters": len(text),
            "bytes": len(text.encode("utf-8")),
            "summary_included": self.summary_included,
            "omitted_messages": self.omitted_messages,
            "omitted_content": self.omitted_content,
            "omitted_caption_fragments": self.omitted_caption_fragments,
            "notice": self.notice,
        }


def _pack(seed: LiveSeed, summary: str = "", *, generated: bool = False) -> LiveSeed:
    note = (
        "Historical context from the selected chat at voice startup. This is a bounded excerpt, not new caller speech. "
        "Tool output, attachments and private reasoning are not included; older details may be omitted. "
        "The main assistant retains the authoritative conversation."
    )
    if seed.task_state:
        note += " " + seed.task_state
    items = [_message("assistant", note)]
    remaining = MAX_SEED_BYTES - len(note.encode("utf-8"))
    clipped = seed.omitted_content
    if summary.strip():
        summary_text = (
            ("Prepared conversation brief" if generated else "Existing conversation summary") + ":\n" + summary
        )
        if len(summary_text.encode("utf-8")) > MAX_SUMMARY_BYTES:
            summary_text = bounded_text(summary_text, MAX_SUMMARY_BYTES - len(_EXCERPT)) + _EXCERPT
            clipped = True
        items.append(_message("assistant", summary_text))
        remaining -= len(summary_text.encode("utf-8"))
    recent: list[Payload] = []
    for item in reversed(seed.recent[-(4 if generated else MAX_RECENT_MESSAGES) :]):
        value = _text(item)
        if not value.strip() or remaining < 128:
            continue
        budget = min(remaining, MAX_MESSAGE_BYTES)
        if len(value.encode("utf-8")) > budget:
            value = bounded_text(value, budget - len(_EXCERPT)) + _EXCERPT
            clipped = True
        recent.append(_message(str(item["role"]), value))
        remaining -= len(value.encode("utf-8"))
    items.extend(reversed(recent))
    omitted = max(0, seed.source_messages - len(recent))
    if not summary and not recent and not seed.task_state:
        items = []
    return replace(
        seed,
        history=tuple(items),
        omitted_messages=omitted,
        omitted_content=clipped or omitted > 0,
        summary_included=bool(summary.strip()),
        notice=seed.notice if items else "No saved chat text was available at voice startup.",
    )


def with_summary(seed: LiveSeed, text: str) -> LiveSeed:
    """Pack a read-only generated brief and the newest four messages under 7 KiB."""
    return _pack(
        replace(
            seed, method="generated_summary", notice="A concise brief and recent chat text were prepared for voice."
        ),
        text,
        generated=True,
    )


def _multimodal() -> str:
    return """
        CASE WHEN json_valid(m.content) THEN
          json_type(m.content) = 'array' AND json_array_length(m.content) > 0 AND NOT EXISTS (
            SELECT 1 FROM json_each(m.content)
            WHERE type != 'object' OR COALESCE(CASE WHEN type = 'object'
              THEN json_extract(value, '$.type') END, '') NOT IN ('text', 'image', 'audio', 'document')
          ) ELSE 0 END
    """


def _visible_text() -> str:
    # SQLite extracts only text parts; attachment bytes and tool arguments never
    # enter Python or the summarizer. Accepted web provenance selects the clean
    # caller request instead of the internally assembled voice/channel prompt.
    return (
        """
        CASE
          WHEN v.transcript IS NOT NULL AND m.role = 'user' THEN v.transcript
          WHEN i.id IS NOT NULL AND m.role = 'user' AND i.channel = '' THEN i.prompt
          WHEN i.id IS NOT NULL AND m.role = 'user' AND i.channel != '' THEN
            CASE WHEN json_valid(substr(i.prompt, ?)) THEN
              CASE WHEN json_type(substr(i.prompt, ?), '$.text') = 'text'
                THEN json_extract(substr(i.prompt, ?), '$.text') ELSE '' END
              ELSE '' END
          WHEN """
        + _multimodal()
        + """ THEN
              (SELECT group_concat(part, char(10)) FROM (
                SELECT substr(json_extract(value, '$.text'), 1, 8193) AS part
                FROM json_each(m.content)
                WHERE type = 'object' AND json_extract(value, '$.type') = 'text'
                  AND json_type(value, '$.text') = 'text' LIMIT 16
              ))
          ELSE m.content
        END
    """
    )


@dataclass
class _SavedText:
    anchor: int
    order: int
    role: str
    text: str
    omitted: bool = False
    voice_session_id: str = ""
    start: float = 0
    end: float = 0


def _group_captions(records: list[_SavedText]) -> list[_SavedText]:
    grouped: list[_SavedText] = []
    for item in sorted(records, key=lambda value: (value.anchor, value.order)):
        last = grouped[-1] if grouped else None
        if (
            last is not None
            and item.voice_session_id
            and item.voice_session_id == last.voice_session_id
            and item.role == last.role
            and 0 <= item.start - last.end <= 2000
            and len((last.text + item.text).encode("utf-8")) <= 8192
        ):
            last.text += item.text
            last.end = item.end
        else:
            grouped.append(item)
    return grouped


def _read(path: Path, session_id: str, mode: ContextMode, task_state: str) -> LiveSeed:
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("BEGIN")
        RoutingStore.root(db, session_id)
        if mode == "none":
            return LiveSeed(mode=mode, chat_session_id=session_id, notice="Voice starts without saved chat context.")
        queued = db.execute(
            "SELECT COUNT(*) FROM ngn_web_inbox WHERE session_id = ? AND status = 'queued'", (session_id,)
        ).fetchone()[0]
        if queued:
            task_state += f" There are {queued} queued assistant requests. Their outcomes are not yet confirmed."
        task_state = task_state.strip()
        boundary = db.execute(
            "SELECT COALESCE(compacted_at_message_id, 0) FROM v2_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        first = int(boundary[0]) if boundary else 0
        # Suppression relies on accepted voice provenance and this root's saved
        # caller captions, never on text equality or a model-authored marker.
        no_duplicate_voice = (
            "NOT (m.role = 'user' AND EXISTS (SELECT 1 FROM ngn_web_voice_origins vo "
            "JOIN ngn_web_live_calls l ON l.voice_session_id = vo.voice_session_id "
            "JOIN ngn_web_live_captions c ON c.voice_session_id = vo.voice_session_id "
            "WHERE vo.history_id = m.id AND l.session_id = m.session_id "
            "AND c.speaker = 'user' AND c.anchor_history_id >= ?))"
        )
        count, watermark = db.execute(
            "SELECT COUNT(*), COALESCE(MAX(m.id), 0) FROM v2_messages m WHERE m.session_id = ? AND m.id >= ? "
            "AND m.role IN ('user', 'assistant') AND m.content IS NOT NULL AND trim(m.content) != '' AND "
            + no_duplicate_voice,
            (session_id, first, first),
        ).fetchone()
        # One bounded snapshot covers the summary, visible recent messages and
        # provenance. It does not resume the Harness or write model history.
        select = (
            "SELECT m.id, m.role, substr(" + _visible_text() + ", 1, 8193), "
            "CASE WHEN " + _multimodal() + " THEN EXISTS (SELECT 1 FROM json_each(m.content) "
            "WHERE json_extract(value, '$.type') != 'text') ELSE 0 END "
            "FROM v2_messages m "
            "LEFT JOIN ngn_web_voice_messages v ON v.history_id = m.id "
            "LEFT JOIN ngn_web_message_origins o ON o.history_id = m.id "
            "LEFT JOIN ngn_web_inbox i ON i.id = o.inbox_id AND i.session_id = m.session_id "
            "WHERE m.session_id = ? AND m.id >= ? "
        )
        prefix = len(_INBOUND_PREFIX) + 1
        rows = db.execute(
            select + "AND m.role IN ('user', 'assistant') AND m.content IS NOT NULL "
            "AND trim(m.content) != '' AND " + no_duplicate_voice + " ORDER BY m.id DESC LIMIT ?",
            (prefix, prefix, prefix, session_id, first, first, MAX_SOURCE_MESSAGES),
        ).fetchall()
        summary_row = db.execute(
            select + "AND m.role = 'compaction_summary' ORDER BY m.id DESC LIMIT 1",
            (prefix, prefix, prefix, session_id, first),
        ).fetchone()
        summary = str(summary_row[2]) if summary_row and isinstance(summary_row[2], str) else ""
        caption_count, caption_watermark = db.execute(
            "SELECT COUNT(*), COALESCE(MAX(c.id), 0) FROM ngn_web_live_captions c "
            "JOIN ngn_web_live_calls l USING (voice_session_id) WHERE l.session_id = ? AND c.anchor_history_id >= ?",
            (session_id, first),
        ).fetchone()
        captions = db.execute(
            "SELECT c.anchor_history_id, c.id, c.speaker, c.text, c.voice_session_id, c.start_ms, c.end_ms "
            "FROM ngn_web_live_captions c JOIN ngn_web_live_calls l USING (voice_session_id) "
            "WHERE l.session_id = ? AND c.anchor_history_id >= ? ORDER BY c.id DESC LIMIT ?",
            (session_id, first, MAX_SOURCE_CAPTIONS),
        ).fetchall()

    records = [
        _SavedText(int(identifier), 0, str(role), value if isinstance(value, str) else "", bool(multimodal))
        for identifier, role, value, multimodal in rows
    ]
    records.extend(
        _SavedText(int(anchor), int(identifier), str(role), str(value), False, str(call), float(start), float(end))
        for anchor, identifier, role, value, call, start, end in captions
    )
    grouped = _group_captions(records)
    source_count = int(count) + sum(bool(item.voice_session_id) for item in grouped)
    recent: list[Payload] = []
    source: list[dict[str, str]] = []
    remaining = MAX_SOURCE_BYTES - 1024
    message_window = max(0, int(count) - len(rows))
    caption_window = max(0, int(caption_count) - len(captions))
    omitted = bool(first or message_window or caption_window)
    source_budget = False
    content_excerpts = False
    if summary:
        value = bounded_text(summary, 12_000)
        source.append({"role": "compaction_summary", "text": value})
        remaining -= len(json.dumps(source[-1], ensure_ascii=False).encode("utf-8")) + 2
        content_excerpts |= value != summary or len(summary) >= 8193
        omitted |= content_excerpts
    newest: list[dict[str, str]] = []
    for record in reversed(grouped):
        role, value = record.role, record.text
        omitted |= record.omitted
        content_excerpts |= record.omitted
        if not isinstance(value, str) or not value.strip():
            omitted = True
            content_excerpts = True
            continue
        text = bounded_text(value, 8192)
        content_excerpts |= text != value or len(value) >= 8193
        omitted |= content_excerpts
        item = {"role": str(role), "text": text}
        if record.voice_session_id:
            item.update(source="live_caption", voice_session_id=record.voice_session_id)
        size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 2
        if size > remaining:
            omitted = True
            source_budget = True
            break
        remaining -= size
        newest.append(item)
        if len(recent) < MAX_RECENT_MESSAGES:
            recent.append(
                _message(str(role), ("Earlier voice transcript:\n" if record.voice_session_id else "") + text)
            )
    source.extend(reversed(newest))

    def serialize() -> str:
        return (
            json.dumps(
                {
                    "messages": source,
                    "task_state": task_state,
                    "partial": omitted,
                    "omissions": {
                        "compacted_history": bool(first),
                        "older_text_messages": message_window,
                        "older_caption_fragments": caption_window,
                        "source_byte_budget": source_budget,
                        "content_excerpts": content_excerpts,
                    },
                    "scope": "Selected user/assistant text and saved voice captions. Tool output, attachments and private reasoning are excluded.",
                },
                ensure_ascii=False,
            )
            if source
            else ""
        )

    serialized = serialize()
    # Bound the serialized source itself, including escapes; complete message
    # objects are removed instead of truncating JSON into an invalid document.
    while len(serialized.encode("utf-8")) > MAX_SOURCE_BYTES and source:
        source.pop(1 if summary and len(source) > 1 else 0)
        omitted = True
        source_budget = True
        serialized = serialize()
    fingerprint = hashlib.sha256(
        json.dumps([session_id, first, watermark, caption_watermark, serialized], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    seed = LiveSeed(
        mode=mode,
        method="existing_summary"
        if mode == "summary" and summary
        else "recent_fallback"
        if mode == "summary"
        else "recent",
        chat_session_id=session_id,
        fingerprint=fingerprint,
        summary_source=serialized,
        recent=tuple(reversed(recent)),
        source_messages=source_count,
        omitted_content=omitted,
        omitted_caption_fragments=max(0, int(caption_count) - len(captions)),
        task_state=task_state,
        notice="Recent chat text and any existing summary were included; omitted details remain with the main assistant.",
    )
    return _pack(seed, summary)


async def read_seed(path: Path, session_id: str, mode: ContextMode, *, task_state: str = "") -> LiveSeed:
    """Read only the admitted root, with bounded memory and no model execution."""
    return await finish_on_cancel(asyncio.to_thread(_read, path, session_id, mode, task_state))
