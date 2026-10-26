"""Run GPT-Live client delegations through the selected web Harness conversation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING
from typing import Literal
from uuid import uuid4

from nagents.live.delegation import ClientDelegationRequest

from .handoff import MAX_HANDOFF_TEXT
from .updates import AssistantUpdates

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents.live.delegation import LiveAppend

    from ..execution.service import Run
    from ..execution.service import WebState

DelegationStatus = Literal["queued", "working", "completed", "failed", "cancelled"]


def _speech_data(transcript: str) -> tuple[list[dict[str, object]], bool]:
    try:
        fragments = json.loads(transcript)
        if not isinstance(fragments, list):
            raise ValueError
        recent: list[dict[str, object]] = []
        length = 0
        for fragment in reversed(fragments):
            if not isinstance(fragment, dict) or fragment.get("speaker") not in {"user", "assistant"}:
                raise ValueError
            text = fragment.get("text")
            if not isinstance(text, str):
                raise ValueError
            if len(text) > 12000:
                raise ValueError
            length += len(text)
            if length > 12000:
                break
            recent.append({key: fragment[key] for key in ("speaker", "text", "start_ms", "end_ms") if key in fragment})
    except (ValueError, TypeError):
        raise ValueError("Live transcript could not be processed; ask the caller to repeat the request") from None
    recent.reverse()
    return recent, len(recent) < len(fragments)


def delegation_prompt(request: ClientDelegationRequest) -> str:
    """Send only the explicit request; captions and IDs stay in inspection data."""
    if not request.text.strip() or len(request.text) > MAX_HANDOFF_TEXT:
        raise ValueError("A native voice handoff needs a bounded request")
    return request.text.strip()


def voice_display(transcript: str, seen: set[str]) -> tuple[str, set[str]]:
    """Project admitted caller speech for the chat without changing model input."""
    fragments: object = json.loads(transcript)
    if not isinstance(fragments, list):
        raise ValueError("Live transcript must contain speech fragments")
    current: set[str] = set()
    new_text: list[str] = []
    latest: list[str] = []
    previous_user = False
    for fragment in fragments:
        if not isinstance(fragment, dict) or fragment.get("speaker") != "user":
            previous_user = False
            continue
        text = fragment.get("text")
        if not isinstance(text, str):
            raise ValueError("Live caller speech must be text")
        if not previous_user:
            latest = []
        previous_user = True
        latest.append(text)
        signature = json.dumps([text, fragment.get("start_ms"), fragment.get("end_ms")], ensure_ascii=False)
        current.add(signature)
        if signature not in seen:
            new_text.append(text)
    display = "".join(new_text if any(part.strip() for part in new_text) else latest).strip()
    return display[-12000:] or "Voice request", current


class MainAgentBridge:
    """Bind a voice call to one chat root while using the host's ordinary run slot."""

    def __init__(
        self,
        state: WebState,
        session_id: str,
        *,
        voice_session_id: str = "",
        report: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.state = state
        self.session_id = session_id
        self.voice_session_id = voice_session_id
        self.report = report
        self._displayed_speech: set[str] = set()
        self._observed: set[str] = set()
        self._request_runs: dict[str, str] = {}
        self._answers: dict[str, str] = {}
        self._submitted: set[str] = set()
        self._pending_requests: set[str] = set()
        self._terminal_requests: set[str] = set()
        self._admission = asyncio.Lock()
        self.updates = AssistantUpdates(state, session_id, self._append_report, self._answer, self._finish_run)
        self.updates.admitted = self._admitted
        self.updates.model = self._model
        self.updates.invalidated = self._invalidate
        self.updates.retain = lambda: bool(self._pending_requests)
        self.updates.known = self._observed

    def attach(self, sink: LiveAppend) -> None:
        if not self.voice_session_id:
            self.voice_session_id = uuid4().hex
        self.updates.voice_session_id = self.voice_session_id
        self.updates.attach(sink)
        self.state.queued_inputs.attach_voice(self.session_id, self)

    async def close(self) -> None:
        self.state.queued_inputs.detach_voice(self.session_id, self)
        for identifier in self._observed - self._submitted - self._pending_requests - self._terminal_requests:
            self._report(identifier, "cancelled", "Voice ended before this request entered the assistant inbox.")
        await self.updates.close()

    def _report(self, identifier: str, phase: str, text: str, run_id: str = "", **details: object) -> None:
        if phase in {"completed", "failed", "cancelled"}:
            self._terminal_requests.add(identifier)
        if self.report is not None:
            self.report(
                {
                    "delegation_id": identifier,
                    "voice_session_id": self.voice_session_id,
                    "chat_session_id": self.session_id,
                    "run_id": run_id,
                    "status": phase,
                    "text": text,
                    **self._target(),
                    **details,
                }
            )

    def _append_report(self, identifier: str, kind: str, text: str, wire: str) -> None:
        if self.report is not None:
            self.report(
                {
                    "delegation_id": identifier,
                    "voice_session_id": self.voice_session_id,
                    "chat_session_id": self.session_id,
                    "live_append": {
                        "kind": kind,
                        "content": text,
                        "wire_type": wire,
                        **({"failed": True} if not wire else {}),
                    },
                }
            )

    def _answer(self, identifier: str, text: str, run_id: str) -> None:
        if self._request_runs.get(identifier) == run_id:
            self._answers[identifier] = text
            self._report(identifier, "working", "An assistant answer is ready for voice.", run_id, result_text=text)

    def _admitted(self, identifier: str, run_id: str, prompt: str) -> None:
        self._request_runs[identifier] = run_id
        self._report(identifier, "working", "Processing…", run_id, request_input=prompt)

    def _model(self, identifier: str, run_id: str, request: dict[str, object]) -> None:
        if self._request_runs.get(identifier) == run_id:
            self._report(identifier, "working", "Assistant model request captured.", run_id, model_request=request)

    def observe(self, request: ClientDelegationRequest) -> None:
        if request.identifier in self._observed or self.updates.detached:
            return
        self._observed.add(request.identifier)
        self._report(request.identifier, "queued", "Waiting for the assistant.", request_transcript=request.transcript)

    def observe_native(self, request: ClientDelegationRequest) -> None:
        self.observe(request)
        if not request.text:
            self._report(request.identifier, "failed", "The native voice handoff had no request. Please repeat it.")

    def _finish_run(self, run: Run) -> None:
        outcome = "completed" if run.outcome == "completed" else "cancelled" if run.outcome == "cancelled" else "failed"
        for identifier, owner in tuple(self._request_runs.items()):
            if owner == run.id:
                self._request_runs.pop(identifier)
                self._pending_requests.discard(identifier)
                self._report(
                    identifier,
                    outcome,
                    "The assistant finished this request.",
                    run.id,
                    **({"result_text": self._answers.pop(identifier)} if identifier in self._answers else {}),
                )

    def _invalidate(self) -> None:
        """A committed root removal also terminates its unexecuted inbox work."""
        pending, self._pending_requests = self._pending_requests, set()
        for identifier in pending:
            self._report(
                identifier,
                "cancelled",
                "The chat was deleted before this request completed.",
                self._request_runs.pop(identifier, ""),
            )
            self._answers.pop(identifier, None)
        self.state.queued_inputs.detach_voice(self.session_id, self)

    async def handle_request(self, request: ClientDelegationRequest) -> str:
        """Admit once; the call-bound event consumer owns all spoken results."""
        if not self.updates.attached:
            raise RuntimeError("Attach the voice update sink before dispatching requests")
        self.observe(request)
        async with self._admission:
            if (
                self.updates.detached
                or request.identifier in self._submitted
                or request.identifier in self._terminal_requests
            ):
                return ""
            if self.updates.failed:
                message = "Voice updates are unavailable. Reconnect voice before making another request; existing work remains in the chat."
                self._report(request.identifier, "failed", message)
                return message
            if request.text:
                explicit = request
            else:
                try:
                    fragments, _ = _speech_data(request.transcript)
                    fresh = [
                        part
                        for part in fragments
                        if part["speaker"] == "user"
                        and json.dumps([part["text"], part.get("start_ms"), part.get("end_ms")], ensure_ascii=False)
                        not in self._displayed_speech
                    ]
                    text = "".join(str(part["text"]) for part in fresh).strip()
                    if not text:
                        if not any(part["speaker"] == "user" and str(part["text"]).strip() for part in fragments):
                            self.updates.append(
                                "commentary", "I don't have your request yet. Could you repeat it?", request.identifier
                            )
                            self._report(
                                request.identifier,
                                "failed",
                                "Caller speech was not available for this delegation. Please repeat the request.",
                            )
                            return ""
                        self.updates.append(
                            "thinking",
                            "This caller request is already with the assistant; wait for its updates.",
                            request.identifier,
                        )
                        self._report(request.identifier, "cancelled", "No new caller request; existing work continues.")
                        return ""
                    explicit = ClientDelegationRequest(request.identifier, text, request.transcript, request.offset_ms)
                except ValueError:
                    self.updates.append("commentary", "Could you repeat that request?", request.identifier)
                    self._report(request.identifier, "failed", "The voice request could not be read.")
                    return ""
            try:
                prompt = delegation_prompt(explicit)
                _, signatures = voice_display(explicit.transcript, self._displayed_speech)
                message_id = (
                    "voice-" + hashlib.sha256((self.voice_session_id + "\0" + request.identifier).encode()).hexdigest()
                )
                self._pending_requests.add(request.identifier)
                await self.state.queued_inputs.submit(
                    self.session_id,
                    message_id,
                    prompt,
                    voice_session_id=self.voice_session_id,
                    voice_delegation_id=request.identifier,
                    voice_display=explicit.text.strip(),
                )
                self._submitted.add(request.identifier)
                self._displayed_speech = signatures
                self.updates.append(
                    "thinking", "The request is queued for the assistant's next response boundary.", request.identifier
                )
                return ""
            except Exception:
                self._pending_requests.discard(request.identifier)
                self._report(
                    request.identifier,
                    "failed",
                    "The assistant could not accept this request. Check the chat and try again.",
                )
                result = "The assistant could not accept this request. Please check the chat before trying again."
            if result:
                self.updates.append("commentary", result, request.identifier)
            return ""

    def _target(self) -> dict[str, str]:
        # Profiles can override the stored UI selection. Read the resolved
        # Harness provider/model that will execute this main-assistant run.
        identity = self.state.session_agents.target(self.session_id)
        return {
            "agent": "Main assistant" if identity["agent"] in {"", "assistant", "default"} else identity["agent"],
            "provider": identity["provider"],
            "model": identity["model"],
        }
