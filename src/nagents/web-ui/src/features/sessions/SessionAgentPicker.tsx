import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../../components/Icon.js";
import { ProviderLogo } from "../../components/ProviderLogo.js";
import { TeamIcon } from "../team/TeamTree.js";
import type { SessionAgentSelection } from "./sessionAgent.js";

export function SessionAgentPicker({ selection, loading, saving, error, choose, refresh, close }: {
  selection?: SessionAgentSelection; loading: boolean; saving: boolean; error: string;
  choose: (name: string) => Promise<boolean>; refresh: () => void; close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null), alive = useRef(false), choosing = useRef(false);
  const titleId = useId(), descriptionId = useId(), searchId = useId();
  const [query, setQuery] = useState(""), [applying, setApplying] = useState("");
  const [localError, setLocalError] = useState("");
  const busy = saving || !!applying;
  const profiles = selection?.profiles || [];
  const needle = query.trim().toLowerCase();
  const matches = profiles.filter(profile => [profile.display_name || profile.name, profile.description, profile.model, profile.provider, profile.provider_kind]
    .some(value => value.toLowerCase().includes(needle)));
  useEffect(() => {
    const element = dialog.current, previous = document.activeElement;
    const overflow = document.documentElement.style.overflow;
    alive.current = true;
    document.documentElement.style.overflow = "hidden";
    element?.showModal();
    element?.querySelector<HTMLElement>("h2")?.focus({ preventScroll: true });
    return () => {
      alive.current = false;
      element?.close();
      document.documentElement.style.overflow = overflow;
      queueMicrotask(() => {
        if (!document.querySelector("dialog[open]") && previous instanceof HTMLElement && previous.isConnected)
          previous.focus({ preventScroll: true });
      });
    };
  }, []);
  async function apply(name: string) {
    if (busy || choosing.current || loading) return;
    choosing.current = true; setApplying(name); setLocalError("");
    try {
      if (await choose(name)) { if (alive.current) close(); }
      else if (alive.current) setLocalError("Could not change this conversation’s agent. Refresh and try again.");
    } catch (cause) {
      if (alive.current) setLocalError(cause instanceof Error ? cause.message : "Could not change this conversation’s agent. Try again.");
    } finally {
      choosing.current = false;
      if (alive.current) setApplying("");
    }
  }
  const active = selection?.active;
  return createPortal(<dialog ref={dialog} className="session-agent-picker" aria-labelledby={titleId} aria-describedby={descriptionId} aria-modal="true"
    onCancel={event => { event.preventDefault(); event.stopPropagation(); if (!busy) close(); }}
    onKeyDown={event => { if (event.key === "Escape") event.stopPropagation(); }}>
    <header className="session-agent-picker-heading"><h2 id={titleId} tabIndex={-1}><TeamIcon size={19} />Agent for this conversation</h2>
      <button type="button" aria-label="Close agent picker" disabled={busy} onClick={close}><Icon name="close" /></button></header>
    <div className="session-agent-picker-body">
      <p id={descriptionId} className="session-agent-picker-description">Choose who answers in this chat. Other chats and the workspace default stay unchanged.</p>
      {selection && <div className="session-agent-current" role="status">
        <span>{selection.pending ? "Next run" : "This conversation"}<strong>{selection.effective.display_name || selection.effective.agent}</strong></span>
        <span className="session-agent-provider"><ProviderLogo provider={selection.effective.provider_kind} />
          <span>{[selection.effective.provider || selection.effective.provider_kind, selection.effective.model].filter(Boolean).join(" · ") || "Model not configured"}</span></span>
        {selection.pending && active && <p>The current run continues as <strong>{active.display_name || active.agent}</strong>. <strong>{selection.effective.display_name || selection.effective.agent}</strong> applies from the next run.</p>}
        {!selection.pending && active && <p>Changes apply after the current run finishes.</p>}
        {active?.mode === "reviewer" && <p>The current run is read-only.</p>}
        {selection.effective.mode === "reviewer" && active?.mode !== "reviewer" && <p>Read-only restrictions apply now.</p>}
      </div>}
      {profiles.length > 6 && <label className="session-agent-search" htmlFor={searchId}><Icon name="search" size={16} /><span className="sr-only">Search agent profiles</span>
        <input id={searchId} type="search" value={query} placeholder="Find an agent…" onInput={event => setQuery(event.currentTarget.value)} /></label>}
      <div className="session-agent-picker-status"><span role="status">{busy ? `Applying ${profiles.find(profile => profile.name === applying)?.display_name || applying || "agent"}…` : loading ? "Loading agent profiles…" : `${profiles.length} profile${profiles.length === 1 ? "" : "s"}`}</span>
        <button type="button" disabled={busy || loading} onClick={() => { setLocalError(""); refresh(); }}>Refresh</button></div>
      {(error || localError) && <p className="session-agent-picker-error" role="alert">{error || localError}</p>}
      <ul className="session-agent-options" aria-label="Agent profiles" aria-busy={loading || busy}>{matches.map(profile => {
        const selected = selection?.agent === profile.name;
        const reason = profile.disabled_reason;
        return <li key={profile.name}><button type="button" className={`session-agent-option${selected ? " is-selected" : ""}`} aria-pressed={selected}
          disabled={busy || loading || !!reason} onClick={() => void apply(profile.name)}>
          <span className="session-agent-option-heading"><strong>{profile.display_name || profile.name}</strong>
            {selection?.workspace_default === profile.name && <span className="session-agent-default">Workspace default</span>}
            <span className="session-agent-choice" aria-hidden="true">{selected ? "✓" : ""}</span></span>
          {profile.description && <span className="session-agent-description">{profile.description}</span>}
          <span className="session-agent-option-meta"><span className="session-agent-provider"><ProviderLogo provider={profile.provider_kind} />
            <span>{[profile.provider || profile.provider_kind, profile.model].filter(Boolean).join(" · ") || "Model not configured"}</span></span>
            {profile.mode === "reviewer" && <span className="session-agent-readonly">Read-only</span>}</span>
          {reason && <span className="session-agent-disabled-reason">{reason}</span>}
        </button></li>;
      })}</ul>
      {!loading && !error && !matches.length && <p className="session-agent-empty">{profiles.length ? "No profiles match your search." : "No agent profiles are available."}</p>}
    </div>
  </dialog>, document.body);
}
