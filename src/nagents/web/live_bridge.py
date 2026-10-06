"""Run GPT-Live client delegations through the selected web Harness conversation."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException

from .live_handoff import MAX_HANDOFF_TEXT
from .live_handoff import LoginHandoff
from .live_inspection import model_requests
from .service import Run

if TYPE_CHECKING:
    from collections.abc import Callable

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
