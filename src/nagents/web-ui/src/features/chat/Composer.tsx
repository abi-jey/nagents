import type { ReactNode, Ref } from "react";

export function Composer({
  inputRef,
  prompt,
  setPrompt,
  demo,
  disabled,
  canSubmit,
  running,
  submit,
  cancel,
  dictation,
  review,
  status,
}: {
  inputRef: Ref<HTMLTextAreaElement>;
  prompt: string;
  setPrompt: (value: string) => void;
  demo: boolean;
  disabled: boolean;
  canSubmit: boolean;
  running: boolean;
  submit: () => void;
  cancel: () => void;
  dictation?: ReactNode;
  review?: ReactNode;
  status?: ReactNode;
}) {
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (canSubmit) submit();
      }}
    >
      <label htmlFor="composer" className="sr-only">
        Message ngn
      </label>
      <textarea
        ref={inputRef}
        id="composer"
        aria-describedby="composer-help"
        placeholder={
          demo
            ? "Ask about the workspace, or try 'demo approval'..."
            : "What should we work on?"
        }
        value={prompt}
        maxLength={32000}
        rows={1}
        disabled={disabled}
        onChange={(event) => setPrompt(event.target.value)}
        onKeyDown={(event) => {
          if (
            event.key === "Enter" &&
            !event.shiftKey &&
            !event.nativeEvent.isComposing
          ) {
            event.preventDefault();
            if (canSubmit) submit();
          }
        }}
      />
      {review}
      <div className="composer-bottom">
        {dictation}
        {status}
        <span className="composer-shortcut" aria-hidden="true">Enter to send · Shift+Enter for a new line</span>
        <details className="composer-help">
          <summary aria-label="Composer help" title="Composer help">?</summary>
          <div className="composer-help-content">
            <p id="composer-help">Enter to send. Shift+Enter for a new line. Resize the text boxes to review longer messages.</p>
            <p>Mic records audio for transcription. Stop uploads it; reaching the time limit does not. Review and insert the text, then use Send.</p>
            <p>{demo ? "Offline demo: no paid requests. Sessions are saved locally." : "Live requests may incur costs. Approvals apply to one call; completed actions are not rolled back. Shell is not sandboxed."}</p>
          </div>
        </details>
        {running ? (
          <button type="button" className="cancel" onClick={cancel}>
            Stop run
          </button>
        ) : (
          <button
            type="submit"
            className="primary"
            disabled={!canSubmit || !prompt.trim()}
          >
            Send
          </button>
        )}
      </div>
    </form>
  );
}
