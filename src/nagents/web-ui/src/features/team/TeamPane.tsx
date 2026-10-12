import { useEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { TeamIcon, TeamTree, type ArrivalTimes } from "./TeamTree.js";
import type { TeamAgent } from "./model.js";
import type { TeamController } from "./useTeam.js";
import { request } from "../../api/client.js";
import { compactLabel, compactionWarning } from "./context.js";
import { contextSummary, contextUsageLabel, contextWindowSource } from "../context/controller.js";

export function TeamPane({ team, agents, arrivals, open, close, configure }: {
  team: TeamController; agents: TeamAgent[]; arrivals: ArrivalTimes; open: boolean; close: () => void; configure: (id: string) => void;
}) {
  const [narrow, setNarrow] = useState(() => typeof matchMedia !== "undefined" && matchMedia("(max-width: 1100px)").matches);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const query = matchMedia("(max-width: 1100px)"); const change = () => { setNarrow(query.matches); if (query.matches) close(); }; query.addEventListener("change", change); return () => query.removeEventListener("change", change); }, []);
  useEffect(() => {
    const element = dialog.current;
    if (!element || !narrow || !open) return;
    const previous = document.activeElement;
    element.showModal();
    return () => { element.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true }); };
  }, [open, narrow]);
  const body = <><header className="team-pane-heading"><h2><TeamIcon size={17} />Team<span>{agents.length}</span></h2><button type="button" aria-label="Close team" onClick={close}><Icon name="close" size={15} /></button></header>
    <TeamTree agents={agents} profiles={team.profiles} selected={team.selected} inspect={id => { team.select(id); if (narrow) close(); }} configure={configure} arrivals={arrivals} error={team.error} refresh={team.refresh} /></>;
  if (narrow) return open ? <dialog ref={dialog} className="team-drawer" aria-label="Team conversations" onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === event.currentTarget) close(); }}>{body}</dialog> : null;
  return open ? <aside className="team-pane" aria-label="Team conversations">{body}</aside> : null;
}

export function AgentConfiguration({ agent, token, sessionId, changed, close }: { agent: TeamAgent; token: string; sessionId: string; changed: () => void; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const pending = agent.task?.next_settings;
  const [provider, setProvider] = useState(pending?.provider || agent.provider);
  const [model, setModel] = useState(pending?.model || agent.model);
  const [trigger, setTrigger] = useState(pending?.compact_trigger || "");
  const [budget, setBudget] = useState(String(pending?.compact_tokens || agent.compactTokens || 200000));
  const [messages, setMessages] = useState(String(pending?.compact_messages || agent.compactMessages || 100));
  const [providers, setProviders] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { const previous = document.activeElement; dialog.current?.showModal(); return () => { dialog.current?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); }; }, []);
  useEffect(() => { const controller = new AbortController(); void request("provider-scopes/workspace/providers", token, undefined, controller.signal).then(response => response.json()).then((result: { providers: Record<string, unknown> }) => setProviders(Object.keys(result.providers))).catch(() => {}); return () => controller.abort(); }, [token]);
  async function save() {
    if (saving) return;
    const tokens = Number(budget), count = Number(messages);
    if (trigger === "tokens" && (!Number.isSafeInteger(tokens) || tokens < 1024 || tokens > 10000000)) { setError("Token budget must be between 1,024 and 10,000,000."); return; }
    if (trigger === "messages" && (!Number.isSafeInteger(count) || count < 1 || count > 10000)) { setError("Message threshold must be between 1 and 10,000."); return; }
    setSaving(true); setError(""); setNotice("");
    try {
      await request(`sessions/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(agent.id)}/settings`, token, {
        provider: provider.trim(), model: model.trim(), compact_trigger: trigger,
        ...(trigger === "tokens" ? { compact_tokens: tokens } : {}), ...(trigger === "messages" ? { compact_messages: count } : {}),
      }, undefined, "PUT");
      changed(); setNotice("Saved for the next idle activation. Current work keeps its accepted configuration.");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not save agent settings."); }
    finally { setSaving(false); }
  }
  return <dialog className="agent-configuration-dialog" ref={dialog} aria-labelledby="agent-config-title" onCancel={event => { event.preventDefault(); close(); }}>
    <header><h2 id="agent-config-title">{agent.name}</h2><button type="button" aria-label="Close agent settings" onClick={close}><Icon name="close" /></button></header>
    <p>Accepted configuration for this agent.</p><dl><dt>Provider</dt><dd>{agent.provider || (agent.providerKind ? `${agent.providerKind} · default connection` : "Unavailable")}</dd><dt>Model</dt><dd>{agent.model || "Unavailable"}</dd>
      <dt>Compaction</dt><dd>{compactLabel(agent) || "Unavailable"}{compactionWarning(agent) && <p className="team-context-warning">{compactionWarning(agent)}</p>}</dd>
      <dt>Context</dt><dd>{agent.context ? <>{contextSummary(agent.context)}<small>{contextUsageLabel(agent.context)} · {contextWindowSource(agent.context)}</small></> : "Unavailable"}</dd>
      <dt>Status</dt><dd>{agent.phase === "waiting" ? agent.reason : agent.phase}</dd>
    </dl><form className="agent-next-settings" onSubmit={event => { event.preventDefault(); void save(); }}>
      <h3>Next activation</h3><p>Saved changes apply when this agent next starts from idle. Messages queued during its current work use the settings above.</p>
      <label>Provider connection<input list="agent-provider-connections" value={provider} onChange={event => setProvider(event.target.value)} maxLength={200} autoComplete="off" /></label>
      <datalist id="agent-provider-connections">{providers.map(id => <option key={id} value={id} />)}</datalist>
      <label>Model<input value={model} onChange={event => setModel(event.target.value)} maxLength={200} autoComplete="off" spellCheck={false} /></label>
      <label>Compaction<select value={trigger} onChange={event => setTrigger(event.target.value)}><option value="">Keep accepted policy</option><option value="auto">Automatic</option><option value="tokens">Token budget</option><option value="messages">Message threshold</option><option value="off">Off</option></select></label>
      {trigger === "tokens" && <label>Context budget<input type="number" min={1024} max={10000000} value={budget} onChange={event => setBudget(event.target.value)} /><small>Compaction starts at 70% of this budget.</small></label>}
      {trigger === "messages" && <label>Messages before compaction<input type="number" min={1} max={10000} value={messages} onChange={event => setMessages(event.target.value)} /></label>}
      {error && <p className="agent-settings-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
      <button type="submit" disabled={saving}>{saving ? "Saving…" : "Save for next activation"}</button>
    </form>
  </dialog>;
}
