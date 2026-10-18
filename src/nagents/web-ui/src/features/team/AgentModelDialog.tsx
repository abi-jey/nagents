import { useEffect, useId, useRef, useState } from "react";
import { request, RequestError } from "../../api/client.js";
import { Icon } from "../../components/Icon.js";
import { ModelBindingPicker } from "../settings/ModelBindingPicker.js";
import { settingsFailure } from "../settings/transport.js";
import type { ModelBinding } from "../settings/types.js";
import { settingsChanged } from "../settings/navigator/state.js";

export type AgentModelReply = {
  agent: string; revision: string; model: ModelBinding | null; definition_model: ModelBinding | null;
  inherited: ModelBinding; effective: ModelBinding; source: "agent" | "workspace" | "global" | "builtin";
  provider_origin: "workspace" | "global" | ""; source_path: string; available: boolean; unavailable_reason: string;
};
const label = (binding: ModelBinding) => [binding.provider || "Startup connection", binding.id].filter(Boolean).join(" / ");

export function AgentModelDialog({ agent, token, changed, close, embedded = false, onDraftChange, onSnapshot }: {
  agent: string; token: string; changed: () => void; close: () => void;
  embedded?: boolean; onDraftChange?: (dirty: boolean) => void; onSnapshot?: (reply: AgentModelReply) => void;
}) {
  const instance = useId();
  const Surface = embedded ? "section" : "dialog";
  const dialog = useRef<HTMLDialogElement>(null);
  const [snapshot, setSnapshot] = useState<AgentModelReply>();
  const [binding, setBinding] = useState<ModelBinding>({ provider: "", id: "" });
  const [mode, setMode] = useState<"inherit" | "custom">("inherit");
  const [dirty, setDirty] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [recovery, setRecovery] = useState(false);
  const [readVersion, refresh] = useState(0);
  const writing = useRef(false);
  const path = `agent-models/${encodeURIComponent(agent)}`;
  function receive(reply: AgentModelReply) {
    setSnapshot(reply); onSnapshot?.(reply);
    const configured = reply.model ?? reply.definition_model;
    setMode(reply.model !== null ? "custom" : "inherit");
    setBinding(configured?.id ? configured : reply.inherited);
    setDirty(false); setError(""); setRecovery(false);
  }
  useEffect(() => { onDraftChange?.(dirty); }, [dirty, onDraftChange]);
  useEffect(() => {
    if (embedded) return;
    const previous = document.activeElement;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setPending(true);
    void request(path, token, undefined, controller.signal).then(response => response.json()).then((reply: AgentModelReply) => {
      if (!controller.signal.aborted) receive(reply);
    }).catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not load agent settings."); })
      .finally(() => { if (!controller.signal.aborted) setPending(false); });
    return () => controller.abort();
  }, [path, token, readVersion]);
  useEffect(() => {
    if (!embedded) return;
    const refreshClean = () => { if (!dirty && !pending && !recovery && !writing.current) refresh(value => value + 1); };
    window.addEventListener(settingsChanged, refreshClean);
    return () => window.removeEventListener(settingsChanged, refreshClean);
  }, [embedded, dirty, pending, recovery]);
  async function save() {
    if (!snapshot || pending || recovery || writing.current) return;
    writing.current = true; setPending(true); setError(""); setNotice("");
    try {
      const reply = await (await request(path, token, { revision: snapshot.revision,
        ...(mode === "custom" ? { model: { provider: binding.provider, id: binding.id.trim() } } : {}),
      }, undefined, mode === "inherit" ? "DELETE" : "PUT")).json() as AgentModelReply;
      receive(reply); changed(); setNotice("Saved. New activations use this model; running agents keep their accepted configuration.");
    } catch (cause) {
      const failure = settingsFailure(cause, true); setError(failure.message); setRecovery(failure.needsRefresh || (cause instanceof RequestError && cause.status === 404));
    } finally { writing.current = false; setPending(false); }
  }
  return <Surface className={`agent-configuration-dialog agent-model-dialog${embedded ? " an-embedded" : ""}`} ref={dialog} aria-labelledby={embedded ? undefined : "agent-model-title"}
    onCancel={event => { event.preventDefault(); if (!pending) close(); }}>
    {!embedded && <header><h2 id="agent-model-title">{agent} model</h2><button type="button" aria-label="Close agent model settings" disabled={pending} onClick={close}><Icon name="close" /></button></header>}
    {snapshot && <>
      <dl><dt>Effective model</dt><dd>{label(snapshot.effective)}</dd><dt>Source</dt><dd>{snapshot.source === "agent" ? "Agent configuration" : snapshot.source === "builtin" ? "Built-in default" : `${snapshot.source === "global" ? "Global" : "Workspace"} default`}{snapshot.provider_origin && ` · ${snapshot.provider_origin} connection`}</dd></dl>
      {!snapshot.available && <p className="error-text" role="status">{snapshot.unavailable_reason || "The configured connection is unavailable."}</p>}
      <p className="agent-model-priority">Agent settings take priority over Workspace, then Global defaults.</p>
      <fieldset className="agent-model-mode" disabled={pending || recovery}><legend>Model for this agent</legend>
        <label><input type="radio" name={embedded ? `${instance}-model-mode` : "agent-model-mode"} checked={mode === "inherit"} onChange={() => { setMode("inherit"); setDirty(true); }} />{snapshot.definition_model ? "Use agent file model" : "Use workspace default"}<small>{label(snapshot.definition_model || snapshot.inherited)}</small></label>
        <label><input type="radio" name={embedded ? `${instance}-model-mode` : "agent-model-mode"} checked={mode === "custom"} onChange={() => { setMode("custom"); setDirty(true); }} />Choose a provider and model</label>
      </fieldset>
      {mode === "custom" && <ModelBindingPicker idPrefix={embedded ? instance : ""} token={token} value={binding} disabled={pending || recovery} named
        change={value => { setBinding(value); setDirty(true); setNotice(""); }} />}
      {snapshot.source_path && <details className="agent-model-source"><summary>Definition</summary><code>{snapshot.source_path}</code>
        <p>{snapshot.definition_model ? `Definition model: ${label(snapshot.definition_model)}` : "The definition inherits the workspace model."}</p></details>}
    </>}
    {pending && !snapshot && <p role="status">Loading model settings…</p>}
    {error && <p className="error-text" role="alert">{error}</p>}
    {(recovery || (!snapshot && error)) && <button type="button" disabled={pending} onClick={() => refresh(value => value + 1)}>Reload saved settings</button>}
    {notice && <p role="status">{notice}</p>}
    <footer className="agent-model-actions"><button type="button" disabled={pending} onClick={embedded ? () => { if (snapshot) { setBinding((snapshot.model ?? snapshot.definition_model)?.id ? (snapshot.model ?? snapshot.definition_model)! : snapshot.inherited); setMode(snapshot.model !== null ? "custom" : "inherit"); setDirty(false); } } : close}>{embedded ? "Discard model draft" : dirty ? "Discard changes" : "Close"}</button>
      <button type="button" className="primary" disabled={pending || recovery || !dirty || (mode === "custom" && (!binding.provider || !binding.id.trim()))} onClick={() => void save()}>{pending && snapshot ? "Saving…" : "Save agent model"}</button></footer>
  </Surface>;
}
