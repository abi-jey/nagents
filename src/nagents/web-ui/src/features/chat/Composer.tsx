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
  attachments,
  hasAttachments = false,
  attachmentTypes = [],
  addAttachments,
  attachmentsDisabled = disabled,
  submitting = false,
  stopping = false,
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
  attachments?: ReactNode;
  hasAttachments?: boolean;
  attachmentTypes?: string[];
  addAttachments?: (files: File[]) => void;
  attachmentsDisabled?: boolean;
  submitting?: boolean;
  stopping?: boolean;
}) {
  return (
    <form
      onPaste={(event) => {
        const files = [...event.clipboardData.files];
        if (files.length && addAttachments && !attachmentsDisabled) { event.preventDefault(); addAttachments(files); }
      }}
      onDragOver={(event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); }}
      onDrop={(event) => { if (event.dataTransfer.files.length) { event.preventDefault(); if (!attachmentsDisabled) addAttachments?.([...event.dataTransfer.files]); } }}
      onSubmit={(event) => {
        event.preventDefault();
        if (canSubmit && (prompt.trim() || hasAttachments)) submit();
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
            if (canSubmit && (prompt.trim() || hasAttachments)) submit();
          }
        }}
      />
      {review}
      {attachments}
      <div className="composer-bottom">
        {!!attachmentTypes.length && <label className="attachment-picker">Attach
          <input type="file" aria-label="Attach images or PDFs" multiple accept={attachmentTypes.join(",")} disabled={attachmentsDisabled}
            onChange={(event) => { addAttachments?.([...event.currentTarget.files || []]); event.currentTarget.value = ""; }} />
        </label>}
        {dictation}
        {status}
        <span id="composer-help" className="sr-only">Enter to send. Shift+Enter for a new line.</span>
        {running && (
          <button type="button" className="cancel" onClick={cancel} disabled={stopping}>
            {stopping ? "Stopping…" : "Stop run"}
          </button>
        )}
        <button
          type="submit"
          className="primary"
          disabled={!canSubmit || (!prompt.trim() && !hasAttachments)}
        >
          {submitting ? "Sending…" : "Send"}
        </button>
      </div>
    </form>
  );
}
