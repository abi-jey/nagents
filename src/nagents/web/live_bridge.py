"""Run GPT-Live client delegations through the selected web Harness conversation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException

from nagents.live.delegation import ClientDelegationRequest

from .live_handoff import MAX_HANDOFF_TEXT
from .live_handoff import LoginHandoff
from .live_inspection import model_requests
from .live_updates import AssistantUpdates
from .service import Run

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents.live.delegation import LiveAppend

    from .service import WebState

# The voice waiter has a shorter budget than the provider's 420-second client
# delegation timeout (including up to 60 seconds waiting for the assistant).
# The application-owned task itself continues until completion or explicit Stop.
VOICE_REPLY_SECONDS = 300.0
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


def voice_prompt(transcript: str) -> str:
    """Keep recent speech as data, preserving speaker order without inventing turns."""
    recent, omitted = _speech_data(transcript)
    if not recent or not any(part["speaker"] == "user" for part in recent):
        raise ValueError("No caller speech is available yet; ask the caller to repeat the request")
    prefix = "Earlier speech is omitted; consult this chat's saved history for previous outcomes. " if omitted else ""
    return (
        "Live voice request for the selected chat. The JSON below contains partial speech fragments, "
        "not verified complete turns. Respond to the latest unresolved caller request, respect corrections "
        "and previously committed chat history, and do not repeat completed actions. "
        "Use your usual tools and approval rules. Return a concise answer for the voice to speak. "
        + prefix
        + "Speech data:\n"
        + json.dumps(recent, ensure_ascii=False)
    )


def native_voice_prompt(request: LoginHandoff) -> str:
    """The native handoff owns the task; captions are optional historical context."""
    if not request.text.strip() or len(request.text) > MAX_HANDOFF_TEXT:
        raise ValueError("A native voice handoff needs a bounded request")
    recent, omitted = _speech_data(request.transcript)
    data: dict[str, object] = {"delegation_id": request.identifier, "request": request.text}
    if request.offset_ms is not None:
        data["offset_ms"] = request.offset_ms
    return (
        "Live voice request for the selected chat. Perform the explicit request in the handoff data below. "
        "The separate speech data is optional historical context captured when this handoff arrived; "
        "it may be partial or absent and must not replace the explicit request with an earlier task. "
        "Respect this chat's saved history and previously completed actions. "
        "Use your usual tools and approval rules. Return a concise answer for the voice to speak. "
        + ("Earlier speech is omitted. " if omitted else "")
        + "Handoff data:\n"
        + json.dumps(data, ensure_ascii=False)
        + "\nSpeech data:\n"
        + json.dumps(recent, ensure_ascii=False)
    )


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
        self._report(identifier, "working", "The queued request reached the assistant.", run_id, request_input=prompt)

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
                prompt = native_voice_prompt(explicit)
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
        config = self.state.harness.config
        return {
            "agent": "Main assistant" if config.agent in {"", "assistant", "default"} else config.agent,
            "provider": config.provider or config.provider_profile().kind,
            "model": self.state.harness.agent.provider.model,
        }

    async def handle(self, transcript: str) -> str:
        return await self._handle(transcript)

    async def handle_native(self, request: LoginHandoff) -> str:
        return await self._handle(request.transcript, request)

    async def _handle(self, transcript: str, native: LoginHandoff | None = None) -> str:
        # This is an application request identity, not a guessed correlation with
        # the provider's notice ID. Public client handlers supply speech only.
        identifier = uuid4().hex
        target = self._target()

        def status(phase: DelegationStatus, text: str, run_id: str = "", **details: object) -> None:
            if self.report is not None:
                self.report(
                    {
                        "delegation_id": identifier,
                        "voice_session_id": self.voice_session_id,
                        "chat_session_id": self.session_id,
                        "run_id": run_id,
                        "status": phase,
                        "text": text,
                        **target,
                        **details,
                    }
                )

        if native is None:
            status("queued", "Waiting for the assistant.", request_transcript=transcript)
        try:
            if native is None:
                prompt = voice_prompt(transcript)
                display, speech = voice_display(transcript, self._displayed_speech)
            else:
                prompt = native_voice_prompt(native)
                display, speech = native.text.strip(), self._displayed_speech
        except ValueError:
            if native is not None:
                status("queued", "Waiting for the assistant.", request_transcript=transcript)
            status("failed", "The voice request could not be read. Please repeat it.")
            return "I need a clear, shorter request. Could you say it again?"
        if native is not None:
            status("queued", "Waiting for the assistant.", request_transcript=transcript, request_input=prompt)
        state = self.state
        try:
            # The sideband can delegate before the provisioning HTTP request
            # releases the host's reservation. Do not lose that first request.
            async with asyncio.timeout(60):
                while state.mutating or state.active is not None or state.harness._busy:
                    if state.channels.closed:
                        status("cancelled", "The assistant shut down before this request started.")
                        return "The assistant is shutting down and cannot take this request."
                    await asyncio.sleep(0.05)
            with state.idle():
                self._displayed_speech = speech
                target = self._target()
                run = Run(self.session_id, server_owned=True, voice=True)
                state.active = run
                state.publish(run, {"event": "run_started", "source": "live_voice"})
                state.status()

                async def execute() -> None:
                    try:

                        def captured(request: dict[str, object]) -> None:
                            text = (
                                "Model request capture limit reached."
                                if request.get("capture_limited")
                                else "Assistant model request captured."
                            )
                            status("working", text, run.id, model_request=request)

                        with model_requests(self.session_id, state.harness.agent.provider, captured):
                            async with state.history.voice_request(
                                self.session_id, run.id, display, voice_session_id=self.voice_session_id
                            ):
                                status(
                                    "working", "The request was sent to the assistant.", run.id, request_input=prompt
                                )
                                await state.produce_session(run, prompt)
                    except asyncio.CancelledError:
                        run.outcome = "cancelled"
                        raise
                    except Exception:
                        run.outcome = "failed"
                        state.publish(
                            run,
                            {"event": "error", "message": "The assistant could not complete this voice request."},
                        )
                    finally:
                        state.harness.session_id = state.selected_session_id
                        state.harness.tools.read_hashes.clear()

                def finished(task: asyncio.Task[None]) -> None:
                    if task.cancelled():
                        run.outcome = "cancelled"
                    state.finish(run)
                    outcome: DelegationStatus = (
                        "completed"
                        if run.outcome == "completed"
                        else "cancelled"
                        if run.outcome == "cancelled"
                        else "failed"
                    )
                    status(
                        outcome,
                        {
                            "completed": "The assistant finished. Its answer is ready.",
                            "cancelled": "The assistant request was stopped.",
                            "failed": "The assistant request failed. Check the chat for details.",
                        }[outcome],
                        run.id,
                        **({"result_text": run.final_text} if outcome == "completed" else {}),
                    )

                run.task = asyncio.create_task(execute(), name=f"ngn-live-backend-{run.id}")
                run.task.add_done_callback(finished)
                status("working", "The assistant is working.", run.id)
        except (HTTPException, TimeoutError):
            status("failed", "The assistant stayed busy; this request did not start.")
            return "The assistant is still busy with another request. Please ask again in a moment."
        except asyncio.CancelledError:
            status("cancelled", "Voice ended before this request started.")
            raise

        # Voice owns this waiter, while WebState owns the assistant task. Ending
        # speech or a voice timeout must not cancel an in-flight workspace action.
        # The normal Stop run control and server shutdown still cancel and join it.
        try:
            async with asyncio.timeout(VOICE_REPLY_SECONDS):
                await asyncio.shield(run.task)
        except TimeoutError:
            if not run.task.done():
                return "The assistant is still working. Follow its progress and results in the chat."
        except asyncio.CancelledError:
            waiter = asyncio.current_task()
            if not run.task.cancelled() or (waiter is not None and waiter.cancelling()):
                raise
            # Stop run cancels the assistant, not the surrounding voice session.
            return "The assistant request was stopped. Check the chat for any actions already completed."
        if run.outcome != "completed":
            return "The assistant could not complete that request. Check the chat for details before trying again."
        if len(run.final_text) > 4000:
            return "The assistant wrote a detailed response in this chat. Please review it there or ask me about a specific part."
        return run.final_text or "The assistant finished without a spoken answer. Check the chat for the result."
