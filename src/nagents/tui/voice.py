"""Terminal voice surfaces; importing the TUI never loads audio/web extras."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.widgets import Static

if TYPE_CHECKING:
    from .voice_bridge import TuiVoiceBridge


@dataclass(frozen=True)
class VoicePrompt:
    text: str
    identifier: str
    session_id: str
    bridge: TuiVoiceBridge


class VoicePanel(Static):
    """A bounded, non-persistent caption preview alongside the normal composer."""

    def __init__(self) -> None:
        super().__init__(id="voice-panel", markup=False)
        self.captions: deque[tuple[str, str]] = deque(maxlen=4)
        self._cursor = 0
        self._session = ""
        self.display = False

    def snapshot(self, payload: dict[str, object], *, muted: bool = False) -> None:
        session = str(payload.get("session_id", ""))
        if session != self._session:
            self._session, self._cursor = session, 0
            self.captions.clear()
        events = payload.get("events", [])
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] <= self._cursor:
                    continue
                self._cursor = event["seq"]
                if event.get("type") != "transcript" or event.get("speaker") not in {"user", "assistant"}:
                    continue
                speaker = "You" if event["speaker"] == "user" else "Voice"
                text = " ".join(str(event.get("text", "")).split())
                if self.captions and self.captions[-1][0] == speaker:
                    previous = self.captions.pop()[1]
                    text = previous + text
                self.captions.append((speaker, text[-1000:]))
        status = str(payload.get("status", "connecting"))
        self.display = True
        self.set_class(status == "error", "error")
        header = f"Voice / {status}{' / mic muted' if muted else ''}  ·  Ctrl+G end  ·  /voice mute"
        if status in {"closed", "error"}:
            header = f"Voice / {payload.get('message', status)}  ·  /voice start"
        self.update("\n".join([header, *(f"{speaker}: {text}" for speaker, text in self.captions)]))
