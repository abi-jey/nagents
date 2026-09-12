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
  edit: (text: string) => void;
  insert: () => void;
  settings: () => void;
};

export function DictationControls(props: DictationControlsProps) {
  const { state, config, unsupported, disabled } = props;
  const active = state.phase !== "idle" && state.phase !== "error";
  const unavailable = unsupported || (!config ? "Open Settings to load dictation availability." :
    !config.available || !config.enabled || !config.admin_enabled ? config.status || "Dictation is unavailable. Check Settings, or keep typing." : "");
  const status = state.phase === "permission" ? "Waiting for microphone permission. You can cancel without changing your draft." :
    state.phase === "recording" ? `Recording. Stop to transcribe, or cancel. Limit: ${config?.max_seconds} seconds.` :
    state.phase === "stopping" ? "Finishing recording…" :
    state.phase === "transcribing" ? "Transcribing with the server's OpenAI API-key connection…" :
    state.phase === "recorded" ? `Recording stopped at the limit (${state.seconds.toFixed(1)} seconds). Nothing uploaded. Choose Transcribe or discard.` :
    state.phase === "review" ? "Transcription ready. Edit it below, then insert into your message draft." :
    state.phase === "idle" ? state.message || unavailable || "Microphone is off." : "";

  return (
    <section className="dictation" aria-label="Microphone dictation" data-phase={state.phase}>
      <div className="dictation-actions">
        {!active && (
          <button type="button" className="dictation-mic" aria-label="Record dictation"
            aria-describedby="dictation-help dictation-status" disabled={disabled || !!unavailable} onClick={props.start}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
              <rect x="9" y="2" width="6" height="12" rx="3" />
              <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4M8 22h8" />
            </svg>
            Mic
          </button>
        )}
        {!active && unavailable && <button type="button" disabled={disabled} onClick={props.settings}>Dictation settings</button>}
        {state.phase === "recording" && <button type="button" className="dictation-stop" onClick={props.stop}>Stop and transcribe</button>}
        {state.phase === "recorded" && <button type="button" onClick={props.transcribe}>Transcribe recording</button>}
        {active && <button type="button" onClick={props.cancel}>{state.phase === "review" || state.phase === "recorded" ? "Discard dictation" : "Cancel dictation"}</button>}
        {state.phase === "error" && <button type="button" onClick={props.cancel}>Dismiss dictation error</button>}
      </div>
      <p id="dictation-help" className="dictation-help">Audio is uploaded only for transcription. Review and insert the text; only Send sends your message to chat.</p>
      <p id="dictation-status" role="status" aria-live="polite" aria-atomic="true">{status}</p>
      {state.phase === "error" && <p role="alert" className="error-text">{state.message}</p>}
      {state.phase === "review" && (
        <div className="dictation-review">
          <label htmlFor="dictation-review-text">Review transcription</label>
          <textarea id="dictation-review-text" value={state.text} rows={3}
            aria-describedby={`dictation-review-help${state.error ? " dictation-review-error" : ""}`}
            aria-invalid={!!state.error} onChange={(event) => props.edit(event.target.value)} />
          <p id="dictation-review-help">Insert appends to your latest draft without replacing your typing. Enter here makes a new line.</p>
          {state.error && <p id="dictation-review-error" role="alert" className="error-text">{state.error}</p>}
          <button type="button" className="dictation-insert" disabled={!state.text.trim()} onClick={props.insert}>Insert into draft</button>
        </div>
      )}
    </section>
  );
}
