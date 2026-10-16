import { useEffect, useRef, useState } from "react";
import { request, RequestError } from "../../api/client.js";
import { Icon } from "../../components/Icon.js";
import { announceSettingsSaved } from "../settings/navigator/state.js";
import { McpDiscovery, type McpDiagnostics } from "./McpDiagnostics.js";

interface ToolInfo {
  name: string; description: string; parameters: Record<string, unknown>;
  category: string; builtin: boolean; reviewer_allowed: boolean;
}
interface SavedToolApproval { tool: string; revision: string; persistent: boolean; }
interface ToolSettings {
  path: string; revision: string; active_agent: string;
  agents: Record<string, Record<string, boolean>>;
  profiles: { id: string; mode: string }[]; tools: ToolInfo[];
  tool_approvals?: SavedToolApproval[];
  mcp_diagnostics?: McpDiagnostics;
  mcp_retry?: { server: string; status: "queued" | "completed" };
}

export function ToolsDialog({ token, blocked, close, embedded = false, onDraftChange }: { token: string; blocked: boolean; close: () => void; embedded?: boolean; onDraftChange?: (dirty: boolean) => void }) {
  const Surface = embedded ? "section" : "dialog";
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const requestRef = useRef<AbortController | undefined>(undefined);
  const writing = useRef(false);
  const retryBusy = useRef(false);
  const alive = useRef(false);
  const queuedRetry = useRef("");
  const [catalog, setCatalog] = useState<ToolSettings>();
  const [agents, setAgents] = useState<ToolSettings["agents"]>({});
  const [agent, setAgent] = useState("");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const [retrying, setRetrying] = useState("");
  const [mcpNotice, setMcpNotice] = useState("");
  const [mcpError, setMcpError] = useState("");
  const disabled = blocked || pending;
  const dirty = !!catalog && JSON.stringify(agents) !== JSON.stringify(catalog.agents);

  useEffect(() => { onDraftChange?.(dirty); }, [dirty, onDraftChange]);

  function receive(next: ToolSettings) {
    setCatalog(next); setAgents(structuredClone(next.agents));
    setAgent((current) => next.profiles.some((profile) => profile.id === current) ? current : next.active_agent);
    setConflict(false);
  }
  function receiveDiscovery(next: ToolSettings) {
    // Resource recovery and background status checks must not overwrite an
    // unsaved tool-selection draft or advance its optimistic write revision.
    setCatalog(current => current ? { ...current, tools: next.tools,
      mcp_diagnostics: next.mcp_diagnostics, tool_approvals: next.tool_approvals } : next);
    if (queuedRetry.current) {
      const server = queuedRetry.current;
      const failed = next.mcp_diagnostics?.failed_servers?.find(item => item.name === server);
      if (!failed?.retry_pending) {
        queuedRetry.current = "";
        setMcpNotice(failed ? `${server} is still unavailable. Check the failure below, then retry when ready.`
          : `${server} connected. Its available tools are listed below.`);
      }
    }
  }
  async function refresh() {
    if (writing.current) return;
    requestRef.current?.abort();
    const controller = new AbortController(); requestRef.current = controller;
    setPending(true); setError(""); setNotice("");
    try { const next = await (await request("tools", token, undefined, controller.signal)).json() as ToolSettings; if (!controller.signal.aborted) receive(next); }
    catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not load tools."); }
    finally { if (!controller.signal.aborted) setPending(false); if (requestRef.current === controller) requestRef.current = undefined; }
  }
  useEffect(() => {
    alive.current = true;
    const element = dialog.current, previous = document.activeElement;
    if (!embedded) { element?.showModal(); heading.current?.focus(); } void refresh();
    const timer = setInterval(() => {
      if (writing.current || requestRef.current) return;
      const controller = new AbortController(); requestRef.current = controller;
      void (async () => {
        try {
          const next = await (await request("tools", token, undefined, controller.signal)).json() as ToolSettings;
          if (!controller.signal.aborted) receiveDiscovery(next);
        } catch { /* Keep the last diagnostic visible; manual refresh reports read errors. */ }
        finally { if (requestRef.current === controller) requestRef.current = undefined; }
      })();
    }, 3000);
    return () => { alive.current = false; clearInterval(timer); requestRef.current?.abort(); if (!embedded) { element?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); } };
  }, []);

  async function save() {
    if (!catalog || disabled || writing.current || conflict || !dirty) return;
    requestRef.current?.abort(); requestRef.current = undefined;
    writing.current = true; setPending(true); setError(""); setNotice("");
    try {
      const next = await (await request("tools", token, { revision: catalog.revision, agents })).json() as ToolSettings;
      receive(next); setNotice(`Saved to ${next.path}. Selections apply to the next model request for each agent.`);
      if (embedded) announceSettingsSaved();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Save was not confirmed. Refresh to check the workspace file.");
      setConflict(cause instanceof RequestError && cause.status === 409);
    } finally { writing.current = false; setPending(false); }
  }
  async function revoke(approval: SavedToolApproval) {
    if (disabled || writing.current) return;
    requestRef.current?.abort(); requestRef.current = undefined;
    writing.current = true; setPending(true); setError(""); setNotice("");
    try {
      const next = await (await request("tools/approvals/revoke", token, { tool: approval.tool, revision: approval.revision })).json() as ToolSettings;
      // Revoking a permission must not discard unsaved enable/disable edits.
      setCatalog((current) => current ? { ...current, tool_approvals: next.tool_approvals } : next);
      setNotice(`${approval.tool} will ask for approval again. Tool selections are unchanged.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Removal was not confirmed. Refresh to check saved permissions.");
    } finally { writing.current = false; setPending(false); }
  }
  async function retryMcp(server: string) {
    if (disabled || writing.current || retryBusy.current) return;
    retryBusy.current = true; setRetrying(server); setMcpError(""); setMcpNotice("");
    queuedRetry.current = "";
    try {
      const next = await (await request("tools/mcp/retry", token, { server })).json() as ToolSettings;
      if (!alive.current) return;
      receiveDiscovery(next);
      queuedRetry.current = next.mcp_retry?.status === "queued" ? server : "";
      const failed = next.mcp_diagnostics?.failed_servers?.find(item => item.name === server);
      setMcpNotice(next.mcp_retry?.status === "queued"
        ? `${server}: retry queued. Current work continues; it will run at the next safe response boundary or message.`
        : failed ? `${server} is still unavailable. Check the failure below, then retry when ready.`
        : `${server} connected. Its available tools are listed below.`);
    } catch (cause) {
      if (alive.current) setMcpError(cause instanceof Error ? cause.message : "Retry was not confirmed. Refresh tools before trying again.");
    } finally { retryBusy.current = false; if (alive.current) setRetrying(""); }
  }
  function choose(names: string[], enabled: boolean) {
    const next = { ...agents[agent] };
    for (const name of names) next[name] = enabled;
    setAgents({ ...agents, [agent]: next }); setNotice("");
  }
  const profile = catalog?.profiles.find((item) => item.id === agent);
  const selected = agents[agent] || {};
  const enabled = (tool: ToolInfo) => selected[tool.name] !== false;
  const visible = (catalog?.tools || []).filter((tool) => (!category || tool.category === category) && `${tool.name} ${tool.description}`.toLowerCase().includes(search.toLowerCase()));
  return <Surface ref={dialog} className={`tools-dialog${embedded ? " an-embedded" : ""}`} aria-labelledby="tools-title" onCancel={(event) => { event.preventDefault(); if (!pending) close(); }}>
    <header><div><h2 id="tools-title" ref={heading} tabIndex={-1}><Icon name="tools" />Tools</h2><p>Choose which tools each agent can use in this workspace.</p></div>
      {!embedded && <button type="button" className="icon-button" aria-label="Close tools" disabled={pending} onClick={close}><Icon name="close" /></button>}</header>
    <div className="tools-toolbar">
      <label>Agent<select value={agent} disabled={disabled} onChange={(event) => setAgent(event.target.value)}>{catalog?.profiles.map((item) => <option key={item.id} value={item.id}>{item.id}{item.id === catalog.active_agent ? " · current" : ""}{item.mode === "reviewer" ? " · read-only" : ""}</option>)}</select></label>
      <label className="tools-search">Search tools<input type="search" placeholder="Name or description…" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
      <label>Category<select value={category} onChange={(event) => setCategory(event.target.value)}><option value="">All tools</option>{[...new Set(catalog?.tools.map((tool) => tool.category))].sort().map((name) => <option key={name}>{name}</option>)}</select></label>
    </div>
    <div className="tools-summary"><span>{catalog ? `${catalog.tools.filter(enabled).length} of ${catalog.tools.length} enabled` : "Loading tools…"}</span>
      <button type="button" disabled={disabled || conflict || !visible.length} onClick={() => choose(visible.map((tool) => tool.name), true)}>Enable visible</button>
      <button type="button" disabled={disabled || conflict || !visible.length} onClick={() => choose(visible.map((tool) => tool.name), false)}>Disable visible</button>
      <button type="button" disabled={disabled || conflict || !catalog} onClick={() => { const next = { ...agents }; delete next[agent]; setAgents(next); }}>Reset agent</button>
    </div>
    {blocked && <p role="status" className="tools-feedback">Finish the current operation before changing tools.</p>}
    {error && <p role="alert" className="tools-feedback error-text">{error}</p>}
    {notice && <p role="status" className="tools-feedback">{notice}</p>}
    <div className="tools-list">
      <McpDiscovery value={catalog?.mcp_diagnostics} retry={server => void retryMcp(server)} retrying={retrying}
        disabled={disabled} notice={mcpNotice} error={mcpError} />
      <section className="tool-saved-approvals" aria-labelledby="tool-saved-approvals-title">
        <h3 id="tool-saved-approvals-title">Saved tool approvals</h3>
        <p>Saved for the approved tool definition across chats and agents in this workspace. Changed tools require approval again. Tool selections and read-only restrictions still apply.</p>
        {catalog?.tool_approvals?.length ? <ul>{catalog.tool_approvals.map((approval) => <li key={approval.tool}>
          <div><code>{approval.tool}</code><small>{approval.persistent ? "Saved across restarts · changes require approval again" : "Saved extension registration · reloads require approval again"}</small></div>
          <button type="button" disabled={disabled} aria-label={`Ask again for ${approval.tool}`} onClick={() => void revoke(approval)}>Ask again</button>
        </li>)}</ul> : <p>{catalog ? "No saved tool approvals. Choose Always allow this tool when a call asks for permission." : "Loading saved permissions…"}</p>}
      </section>
      {visible.map((tool) => <article className={`tool-setting${enabled(tool) ? " enabled" : ""}`} key={tool.name}>
      <div className="tool-setting-heading"><div><h3>{tool.name}</h3><span className="tool-category">{tool.category}</span></div>
        <label className="tool-switch"><span>{enabled(tool) ? "Enabled" : "Disabled"}</span><input type="checkbox" role="switch" aria-label={`Enable ${tool.name} for ${agent}`} checked={enabled(tool)} disabled={disabled || conflict} onChange={(event) => choose([tool.name], event.target.checked)} /></label></div>
      <p>{tool.description}</p>
      {profile?.mode === "reviewer" && !tool.reviewer_allowed && <p className="tool-restriction">This agent's read-only mode blocks execution of this tool.</p>}
      <details><summary>Tool schema · read-only</summary><pre>{JSON.stringify(tool.parameters, null, 2)}</pre></details>
    </article>)}{catalog && !visible.length && <p className="tools-empty">No tools match your search.</p>}</div>
    <footer><div><code>{catalog?.path || ".ngn/tools.yaml"}</code><small>{dirty ? "Unsaved changes" : "Workspace configuration"}</small></div>
      <button type="button" disabled={pending} onClick={() => void refresh()}>{conflict ? "Reload saved settings" : "Refresh"}</button>
      <button type="button" className="primary" disabled={disabled || conflict || !dirty} onClick={() => void save()}>Save tools</button></footer>
  </Surface>;
}
