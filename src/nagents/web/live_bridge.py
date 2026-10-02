"""Run GPT-Live client delegations through the selected web Harness conversation."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from fastapi import HTTPException

from .service import Run

if TYPE_CHECKING:
    from .service import WebState


def voice_prompt(transcript: str) -> str:
    """Keep recent speech as data, preserving speaker order without inventing turns."""
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
    if not recent or not any(part["speaker"] == "user" for part in recent):
        raise ValueError("No caller speech is available yet; ask the caller to repeat the request")
    recent.reverse()
    prefix = (
        "Earlier speech is omitted; consult this chat's saved history for previous outcomes. "
        if len(recent) < len(fragments)
        else ""
    )
    return (
        "Live voice request for the selected chat. The JSON below contains partial speech fragments, "
        "not verified complete turns. Respond to the latest unresolved caller request, respect corrections "
        "and previously committed chat history, and do not repeat completed actions. "
        "Use your usual tools and approval rules. Return a concise answer for the voice to speak. "
        + prefix
        + "Speech data:\n"
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

    def __init__(self, state: WebState, session_id: str, *, voice_session_id: str = "") -> None:
        self.state = state
        self.session_id = session_id
        self.voice_session_id = voice_session_id
        self._displayed_speech: set[str] = set()

    async def handle(self, transcript: str) -> str:
        try:
            prompt = voice_prompt(transcript)
            display, speech = voice_display(transcript, self._displayed_speech)
        except ValueError:
            return "I need a clear, shorter request. Could you say it again?"
        state = self.state
        try:
            # The sideband can delegate before the provisioning HTTP request
            # releases the host's reservation. Do not lose that first request.
            async with asyncio.timeout(60):
                while state.mutating or state.active is not None or state.harness._busy:
                    if state.channels.closed:
                        return "The assistant is shutting down and cannot take this request."
                    await asyncio.sleep(0.05)
            with state.idle():
                self._displayed_speech = speech
                run = Run(self.session_id, server_owned=True, voice=True)
                state.active = run
                state.publish(run, {"event": "run_started", "source": "live_voice"})
                state.status()

                async def execute() -> None:
                    try:
                        async with state.history.voice_request(
                            self.session_id, run.id, display, voice_session_id=self.voice_session_id
                        ):
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

                run.task = asyncio.create_task(execute(), name=f"ngn-live-backend-{run.id}")
                run.task.add_done_callback(finished)
        except (HTTPException, TimeoutError):
            return "The assistant is still busy with another request. Please ask again in a moment."

        # Voice owns this waiter, while WebState owns the assistant task. Ending
        # speech or a voice timeout must not cancel an in-flight workspace action.
        # The normal Stop run control and server shutdown still cancel and join it.
        try:
            await asyncio.shield(run.task)
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
