import { useLayoutEffect, useRef } from "react";
import type { ComponentProps, ReactNode, Ref } from "react";

function ComposerInput({ inputRef, ...props }: ComponentProps<"textarea"> & { inputRef: Ref<HTMLTextAreaElement> }) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  function resize() {
    const element = textarea.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(160, Math.max(32, element.scrollHeight))}px`;
  }
  useLayoutEffect(resize, [props.value]);
  useLayoutEffect(() => {
    const element = textarea.current;
    if (!element) return;
    let width = element.getBoundingClientRect().width;
    const observer = new ResizeObserver(() => {
      const next = element.getBoundingClientRect().width;
      if (next !== width) { width = next; resize(); }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return <textarea {...props} ref={(element) => {
    textarea.current = element;
    if (typeof inputRef === "function") return inputRef(element);
    if (inputRef) inputRef.current = element;
  }} />;
}

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
      <ComposerInput
        inputRef={inputRef}
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
        <span id="composer-help" className="sr-only">Enter to send. Shift+Enter for a new line.</span>
        {running && (
          <button type="button" className="cancel" onClick={cancel}>
            Stop run
          </button>
        )}
        <button
          type="submit"
          className="primary"
          disabled={!canSubmit || !prompt.trim()}
        >
          Send
        </button>
      </div>
    </form>
  );
}
