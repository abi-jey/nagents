import { useLayoutEffect, useRef, useState } from "react";
import type { ComponentProps, ReactNode, Ref } from "react";
import { Icon } from "../../components/Icon.js";
import { commandMatches } from "./commands.js";
import type { SkillItem } from "./skills.js";

export interface ComposerVoiceRefs {
  presence: Ref<HTMLDivElement>;
  caption: Ref<HTMLDivElement>;
  controls: Ref<HTMLDivElement>;
  details: Ref<HTMLDivElement>;
}

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
  status,
  attachments,
  hasAttachments = false,
  attachmentTypes = [],
  addAttachments,
  attachmentsDisabled = disabled,
  submitting = false,
  stopping = false,
  voiceControl,
  voiceLayout = false,
  voiceRefs,
  skills = [],
  skillsLoading = false,
  skillsError = "",
  refreshSkills,
  recipient = "ngn",
  commandsEnabled = true,
  maxLength = 32000,
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
  status?: ReactNode;
  attachments?: ReactNode;
  hasAttachments?: boolean;
  attachmentTypes?: string[];
  addAttachments?: (files: File[]) => void;
  attachmentsDisabled?: boolean;
  submitting?: boolean;
  stopping?: boolean;
  voiceControl?: ReactNode;
  voiceLayout?: boolean;
  voiceRefs?: ComposerVoiceRefs;
  skills?: SkillItem[];
  skillsLoading?: boolean;
  skillsError?: string;
  refreshSkills?: () => void;
  recipient?: string;
  commandsEnabled?: boolean;
  maxLength?: number;
}) {
  const [dismissed, setDismissed] = useState("");
  const [selection, setSelection] = useState({ prompt: "", index: 0 });
  const suggesting = commandsEnabled && !disabled && prompt !== dismissed && /^\/[A-Za-z0-9:_-]*$/.test(prompt);
  const matches = suggesting ? commandMatches(prompt, skills) : [];
  const selected = selection.prompt === prompt ? Math.min(selection.index, matches.length - 1) : 0;
  useLayoutEffect(() => {
    const command = matches[selected];
    if (command) document.getElementById(`slash-command-${command.name}`)?.scrollIntoView?.({ block: "nearest" });
  }, [prompt, selected, matches.length]);
  function choose(index: number) {
    const command = matches[index];
    if (!command) return;
    setPrompt(`/${command.name} `);
    setDismissed("");
  }
  return (
    <form
      className={`chat-composer${voiceLayout ? " has-live-voice" : ""}`}
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
      {suggesting && (!!matches.length || skillsLoading || skillsError) && <div className="slash-menu">
      {!!matches.length && <ul id="slash-commands" role="listbox" aria-label="Commands and skills">
        {matches.map((command, index) => <li key={command.name} role="presentation"><button type="button"
          id={`slash-command-${command.name}`} role="option" aria-selected={index === selected} tabIndex={-1}
          onPointerDown={event => event.preventDefault()} onClick={() => choose(index)}>
          <span className="slash-command-name"><code>/{command.name}</code>{command.skill && <span className="slash-kind">Skill</span>}</span><span>{command.description}</span>
        </button></li>)}
      </ul>}
      {(skillsLoading || skillsError) && <div className="slash-catalog-status"><span role="status">{skillsError || "Loading skills…"}</span>
        {!!skillsError && refreshSkills && <button type="button" onClick={refreshSkills}>Retry</button>}</div>}
      </div>}
      <div className="composer-exchange">
      <div className="composer-voice-presence" ref={voiceRefs?.presence} hidden={!voiceLayout} />
      <div className="composer-writing">
      <div className="composer-voice-caption" ref={voiceRefs?.caption} hidden={!voiceLayout} />
      <label htmlFor="composer" className="sr-only">
        Message {recipient}
      </label>
      <ComposerInput
        inputRef={inputRef}
        id="composer"
        aria-describedby="composer-help"
        aria-autocomplete="list"
        aria-controls={matches.length ? "slash-commands" : undefined}
        aria-activedescendant={matches[selected] ? `slash-command-${matches[selected].name}` : undefined}
        placeholder={
          !commandsEnabled ? `Message ${recipient}…` : demo
            ? "Ask about the workspace, or try 'demo approval'..."
            : "What should we work on? Type / for commands"
        }
        value={prompt}
        maxLength={maxLength}
        rows={1}
        disabled={disabled}
        onChange={(event) => { setDismissed(""); setPrompt(event.target.value); }}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (suggesting && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey) {
            if (event.key === "Escape") { event.preventDefault(); setDismissed(prompt); return; }
            if (matches.length && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
              event.preventDefault();
              setSelection({ prompt, index: (selected + (event.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length });
              return;
            }
            if (matches.length && (event.key === "Tab" || (event.key === "Enter" && prompt !== `/${matches[selected]?.name}`))) { event.preventDefault(); choose(selected); return; }
          }
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
      </div>
      </div>
      {attachments}
      <div className="composer-bottom">
        <div className="composer-tools">
        {!!attachmentTypes.length && <label className="attachment-picker"><Icon name="paperclip" size={17} /><span>Attach</span>
          <input type="file" aria-label="Attach files" multiple accept={attachmentTypes.join(",")} disabled={attachmentsDisabled}
            onChange={(event) => { addAttachments?.([...event.currentTarget.files || []]); event.currentTarget.value = ""; }} />
        </label>}
        {voiceControl}
        <div className="composer-voice-controls" ref={voiceRefs?.controls} hidden={!voiceLayout} />
        </div>
        {status}
        <span id="composer-help" className="sr-only">Enter to send. Shift+Enter for a new line.{commandsEnabled && " Type / for commands; use arrows and Tab or Enter to choose, then Enter to run. Escape closes suggestions."}</span>
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
      <div className="composer-voice-details" ref={voiceRefs?.details} hidden={!voiceLayout} />
    </form>
  );
}
