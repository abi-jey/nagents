import { useEffect, useState } from "react";
import type { DictationConfig, DictationState } from "./types.js";

export type DictationControlsProps = {
  state: DictationState;
  config?: DictationConfig;
  unsupported: string;
  disabled: boolean;
  start: () => void;
  stop: () => void;
  cancel: () => void;
  transcribe: () => void;
};

function RecordingTime() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const start = performance.now();
    const timer = setInterval(() => setSeconds(Math.floor((performance.now() - start) / 1000)), 1000);
    return () => clearInterval(timer);
  }, []);
  // The phase has its own live announcement; the visual clock must not spam it.
  return <span aria-hidden="true">Rec {Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}</span>;
}

export function DictationControls(props: DictationControlsProps) {
  const { state, config } = props;
  const active = state.phase !== "idle" && state.phase !== "error";
  const status = state.phase === "permission" ? "Waiting for microphone permission. Cancel is available." :
    state.phase === "recording" ? `Recording. Stop to transcribe, or cancel. Limit: ${config?.max_seconds} seconds.` :
    state.phase === "stopping" ? "Finishing recording…" :
    state.phase === "transcribing" ? "Transcribing with the server's OpenAI API-key connection…" :
    state.phase === "recorded" ? `Recording stopped at the limit (${state.seconds.toFixed(1)} seconds). Nothing uploaded. Choose Transcribe or discard.` :
    state.phase === "review" ? "Transcription ready. Review, insert into your draft, then Send." :
    state.phase === "idle" ? state.message : "";

  if (state.phase === "idle") return state.message ? <span id="dictation-status" className="sr-only" role="status" aria-live="polite" aria-atomic="true">{state.message}</span> : null;
  return (
    <div className="dictation-actions" role="group" aria-label="Microphone dictation" data-phase={state.phase}>
      <span className="dictation-phase" aria-hidden="true">
        {state.phase === "recording" ? <RecordingTime /> :
          state.phase === "permission" ? "Permission…" :
          state.phase === "stopping" ? "Finishing…" :
          state.phase === "transcribing" ? "Transcribing…" :
          state.phase === "recorded" ? "At limit" : ""}
      </span>
      {state.phase === "recording" && <button type="button" className="dictation-stop" aria-label="Stop and transcribe" title="Stop and transcribe" onClick={props.stop}>Stop</button>}
      {state.phase === "recorded" && <button type="button" aria-label="Transcribe recording" title="Transcribe recording" onClick={props.transcribe}>Transcribe</button>}
      {active && state.phase !== "review" && (
        <button type="button" className="icon-button" aria-label={state.phase === "recorded" ? "Discard dictation" : "Cancel dictation"}
          title={state.phase === "recorded" ? "Discard dictation" : "Cancel dictation"} onClick={props.cancel}>
          <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" /></svg>
        </button>
      )}
      <span id="dictation-help" className="sr-only">Audio is uploaded only for transcription. Review and insert the text; only Send sends your message to chat.</span>
      <span id="dictation-status" className="sr-only" role="status" aria-live="polite" aria-atomic="true">{status}</span>
      {state.phase === "error" && (
        <details className="dictation-error" open>
          <summary>Dictation error</summary>
          <p role="alert">{state.message}</p>
          <button type="button" onClick={props.cancel}>Dismiss dictation error</button>
        </details>
      )}
    </div>
  );
}

export function DictationReview({ state, edit, insert, cancel }: {
  state: DictationState;
  edit: (text: string) => void;
  insert: () => void;
  cancel: () => void;
}) {
  if (state.phase !== "review") return null;
  return (
    <section className="dictation-review" aria-label="Transcription review">
      <textarea id="dictation-review-text" value={state.text} rows={2}
        aria-describedby={`dictation-review-help${state.error ? " dictation-review-error" : ""}`}
        aria-invalid={!!state.error} onChange={(event) => edit(event.target.value)} />
      <div className="dictation-review-actions">
        <label htmlFor="dictation-review-text">Review transcription</label>
        <button type="button" className="dictation-insert" aria-label="Insert into draft" title="Insert into draft" disabled={!state.text.trim()} onClick={insert}>Insert</button>
        <button type="button" aria-label="Discard dictation" title="Discard dictation" onClick={cancel}>Discard</button>
      </div>
      <p id="dictation-review-help" className="sr-only">Insert appends to your latest draft without replacing your typing. Enter here makes a new line. Resize or scroll to read longer text.</p>
      {state.error && <p id="dictation-review-error" role="alert" className="error-text">{state.error}</p>}
    </section>
  );
}
