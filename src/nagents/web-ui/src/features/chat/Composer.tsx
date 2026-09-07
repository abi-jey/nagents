import type { Ref } from "react";

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
}) {
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <label htmlFor="composer" className="sr-only">
        Message ngn
      </label>
      <textarea
        ref={inputRef}
        id="composer"
        placeholder={
          demo
            ? "Ask about the workspace, or try 'demo approval'..."
            : "What should we work on?"
        }
        value={prompt}
        maxLength={32000}
        rows={2}
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
      <div className="composer-bottom">
        <span>
          Enter to send <b>/</b> Shift+Enter for newline
        </span>
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
            Send <span aria-hidden="true">&#8593;</span>
          </button>
        )}
      </div>
    </form>
  );
}
