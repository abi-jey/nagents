import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { DEMO_CATALOG_NOTE, DEMO_PROVIDERS, resolveRuntimeSettings, validateRuntimeSettings } from "./agentRuntime";
import type { CompactionTrigger, DemoProviderId, RuntimeSettings } from "./agentRuntime";
import type { AgentRun } from "./types";
import "./agent-settings.css";

export interface AgentSettingsDialogProps {
  agent: AgentRun;
  settings: RuntimeSettings;
  onSave: (settings: RuntimeSettings) => void;
  onClose: () => void;
}

/** Mounted for one agent at a time; drafts become next-run overrides only after Save. */
export function AgentSettingsDialog({ agent, settings, onSave, onClose }: AgentSettingsDialogProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState<RuntimeSettings>(() => ({ ...settings }));
  const [submitted, setSubmitted] = useState(false);
  const id = useId();
  const inherited = resolveRuntimeSettings(agent);
  const validation = validateRuntimeSettings(agent, draft);
  const effective = validation.valid ? resolveRuntimeSettings(agent, draft) : undefined;
  const selectedProvider = DEMO_PROVIDERS.find(provider => provider.id === (draft.provider ?? inherited.provider)) ?? DEMO_PROVIDERS[0];
  const mode = draft.compact_trigger ?? inherited.compact_trigger;
  const errors = submitted ? validation.errors : {};
  useEffect(() => {
    const node = dialog.current;
    const opener = document.activeElement;
    if (node && !node.open) node.showModal();
    return () => {
      if (node?.open) node.close();
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus({ preventScroll: true });
    };
  }, []);
  function change<K extends keyof RuntimeSettings>(key: K, value: RuntimeSettings[K]) {
    setDraft(current => {
      const next = { ...current };
      if (value === undefined) delete next[key]; else next[key] = value;
      return next;
    });
  }
  function changeProvider(value: string) {
    setDraft(current => {
      const next = { ...current };
      delete next.model;
      if (value) next.provider = value as DemoProviderId; else delete next.provider;
      return next;
    });
  }
  function revealOverride(field: "tokens" | "messages") {
    change("compact_trigger", field);
    window.requestAnimationFrame(() => {
      const input = document.getElementById(`${id}-${field}`);
      input?.focus({ preventScroll: true });
      input?.scrollIntoView({ block: "nearest" });
    });
  }
  function save(event: FormEvent) {
    event.preventDefault();
    setSubmitted(true);
    if (!validation.valid) return;
    onSave({ ...draft });
    onClose();
  }
  return <dialog ref={dialog} className="agent-settings-dialog" aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`} onCancel={event => { event.preventDefault(); onClose(); }} onClick={event => {
    if (event.target !== event.currentTarget) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
  }}><form onSubmit={save} noValidate>
    <header className="agent-settings-heading"><div><span className="eyebrow">Agent settings · next run</span><h2 id={`${id}-title`}>{agent.definition.name}</h2></div><button type="button" className="icon-button" aria-label={`Close ${agent.definition.name} settings`} onClick={onClose}><svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" /></svg></button></header>
    <div className="agent-settings-body"><p id={`${id}-description`} className="agent-settings-intro">Save this agent’s defaults, then restart the scenario to use them. The current run keeps its accepted provider, model, and compaction policy.</p>
      <fieldset className="agent-settings-group"><legend>Model connection</legend><div className="agent-settings-field"><label htmlFor={`${id}-provider`}>Provider</label><select id={`${id}-provider`} autoFocus value={draft.provider ?? ""} onChange={event => changeProvider(event.target.value)} aria-invalid={Boolean(errors.provider)} aria-describedby={errors.provider ? `${id}-provider-error` : undefined}><option value="">Inherit · {inherited.providerLabel}</option>{DEMO_PROVIDERS.map(provider => <option key={provider.id} value={provider.id}>{provider.label}</option>)}</select>{errors.provider && <p id={`${id}-provider-error`} className="agent-settings-error">{errors.provider}</p>}</div>
        <div className="agent-settings-field"><label htmlFor={`${id}-model`}>Model</label><select id={`${id}-model`} value={draft.model ?? ""} onChange={event => change("model", event.target.value || undefined)} aria-invalid={Boolean(errors.model)} aria-describedby={errors.model ? `${id}-model-error` : undefined}><option value="">Inherit · {selectedProvider.id === inherited.provider ? inherited.model : selectedProvider.models[0].label}</option>{selectedProvider.models.map(model => <option key={model.id} value={model.id}>{model.label}</option>)}</select>{errors.model && <p id={`${id}-model-error`} className="agent-settings-error">{errors.model}</p>}</div>
        <div className="agent-settings-window"><span>Model context window</span><strong>{(effective?.modelContextLimit ?? selectedProvider.models[0].contextLimit).toLocaleString("en-US")} tokens</strong><small>Read-only demo catalog value</small></div><p className="agent-settings-help">{DEMO_CATALOG_NOTE}</p></fieldset>
      <fieldset className="agent-settings-group"><legend>Context & compaction</legend><div className="agent-settings-field"><label htmlFor={`${id}-policy`}>Compaction policy</label><select id={`${id}-policy`} value={draft.compact_trigger ?? ""} onChange={event => change("compact_trigger", event.target.value ? event.target.value as CompactionTrigger : undefined)}><option value="">Inherit · Auto</option><option value="auto">Auto · 70% of model window</option><option value="tokens">Token budget</option><option value="messages">Message count</option><option value="off">Off</option></select></div>
        {mode === "tokens" && <div className="agent-settings-field"><label htmlFor={`${id}-tokens`}>Context token budget <span>Optional override</span></label><input id={`${id}-tokens`} type="number" inputMode="numeric" min={1024} max={10_000_000} step={1} value={draft.compact_tokens ?? ""} placeholder={`Inherit · ${inherited.compact_tokens.toLocaleString("en-US")}`} onChange={event => change("compact_tokens", event.target.value === "" ? undefined : Number(event.target.value))} aria-invalid={Boolean(errors.compact_tokens)} aria-describedby={`${id}-tokens-help${errors.compact_tokens ? ` ${id}-tokens-error` : ""}`} /><p id={`${id}-tokens-help`} className="agent-settings-help">Compaction triggers at 70% of this budget{effective ? ` (${Math.floor(effective.compact_tokens * 0.7).toLocaleString("en-US")} tokens)` : ""}. This does not change the model’s context window.</p>{errors.compact_tokens && <p id={`${id}-tokens-error`} className="agent-settings-error">{errors.compact_tokens}</p>}</div>}
        {mode === "messages" && <div className="agent-settings-field"><label htmlFor={`${id}-messages`}>Context message limit <span>Optional override</span></label><input id={`${id}-messages`} type="number" inputMode="numeric" min={1} max={10_000} step={1} value={draft.compact_messages ?? ""} placeholder={`Inherit · ${inherited.compact_messages}`} onChange={event => change("compact_messages", event.target.value === "" ? undefined : Number(event.target.value))} aria-invalid={Boolean(errors.compact_messages)} aria-describedby={`${id}-messages-help${errors.compact_messages ? ` ${id}-messages-error` : ""}`} /><p id={`${id}-messages-help`} className="agent-settings-help">Counts accepted context messages, including completed tool results. Queued notes wait for the next response.</p>{errors.compact_messages && <p id={`${id}-messages-error`} className="agent-settings-error">{errors.compact_messages}</p>}</div>}
        {mode === "auto" && <p className="agent-settings-help">Uses the selected model’s illustrative window. The status pane separates estimated context from the compaction threshold.</p>}{mode === "off" && <p className="agent-settings-help">Automatic compaction is disabled for the next run. The model context window still applies.</p>}
        <p className="agent-settings-boundary">A due compaction belongs before a model response, after the current tool batch. This design demo shows the policy and threshold; it does not compact messages.</p></fieldset>
      {submitted && !validation.valid && <div className="agent-settings-error-summary" role="alert"><strong>Check these saved overrides</strong><ul>{Object.entries(validation.errors).map(([field, message]) => <li key={field}><span>{message}</span>{field === "compact_tokens" && mode !== "tokens" && <button type="button" onClick={() => revealOverride("tokens")}>Edit token budget</button>}{field === "compact_messages" && mode !== "messages" && <button type="button" onClick={() => revealOverride("messages")}>Edit message limit</button>}</li>)}</ul></div>}
      <div className="agent-settings-next"><span className="eyebrow">Next run</span><strong>{effective ? `${effective.providerLabel} · ${effective.model}` : "Check the override values"}</strong><span>{Object.keys(draft).length ? `${Object.keys(draft).length} explicit ${Object.keys(draft).length === 1 ? "override" : "overrides"}` : "Inherits the original agent profile"}</span></div>
      <details className="agent-settings-json"><summary>Saved override fields</summary><pre>{JSON.stringify(draft, null, 2)}</pre></details>
    </div><footer className="agent-settings-footer"><button type="button" className="settings-inherit" onClick={() => { setDraft({}); setSubmitted(false); }}>Reset to inherited</button><div><button type="button" onClick={onClose}>Cancel</button><button type="submit" className="settings-save">Save for next run</button></div></footer>
  </form></dialog>;
}
