import { useEffect, useRef, useState } from "react";
import { request, RequestError } from "../../api/client";
import { Icon } from "../../components/Icon";
import "./tools.css";

interface ToolInfo {
  name: string; description: string; parameters: Record<string, unknown>;
  category: string; builtin: boolean; reviewer_allowed: boolean; alias_of: string;
}
interface ToolSettings {
  path: string; revision: string; active_agent: string;
  agents: Record<string, Record<string, boolean>>;
  profiles: { id: string; mode: string }[]; tools: ToolInfo[];
}

export function ToolsDialog({ token, blocked, close }: { token: string; blocked: boolean; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const requestRef = useRef<AbortController | undefined>(undefined);
  const writing = useRef(false);
  const [catalog, setCatalog] = useState<ToolSettings>();
  const [agents, setAgents] = useState<ToolSettings["agents"]>({});
  const [agent, setAgent] = useState("");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const disabled = blocked || pending;
  const dirty = !!catalog && JSON.stringify(agents) !== JSON.stringify(catalog.agents);

  function receive(next: ToolSettings) {
    setCatalog(next); setAgents(structuredClone(next.agents));
    setAgent((current) => next.profiles.some((profile) => profile.id === current) ? current : next.active_agent);
    setConflict(false);
  }
  async function refresh() {
    if (writing.current) return;
    requestRef.current?.abort();
    const controller = new AbortController(); requestRef.current = controller;
    setPending(true); setError(""); setNotice("");
    try { const next = await (await request("tools", token, undefined, controller.signal)).json() as ToolSettings; if (!controller.signal.aborted) receive(next); }
    catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not load tools."); }
    finally { if (!controller.signal.aborted) setPending(false); }
  }
  useEffect(() => {
    const element = dialog.current, previous = document.activeElement;
    element?.showModal(); heading.current?.focus(); void refresh();
    return () => { requestRef.current?.abort(); element?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);

  async function save() {
    if (!catalog || disabled || writing.current || conflict || !dirty) return;
    writing.current = true; setPending(true); setError(""); setNotice("");
    try {
      const next = await (await request("tools", token, { revision: catalog.revision, agents })).json() as ToolSettings;
      receive(next); setNotice(`Saved to ${next.path}. Selections apply to the next model request for each agent.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Save was not confirmed. Refresh to check the workspace file.");
      setConflict(cause instanceof RequestError && cause.status === 409);
    } finally { writing.current = false; setPending(false); }
  }
  function choose(names: string[], enabled: boolean) {
    const next = { ...agents[agent] };
    for (const name of names) next[name] = enabled;
    setAgents({ ...agents, [agent]: next }); setNotice("");
  }
  const profile = catalog?.profiles.find((item) => item.id === agent);
  const selected = agents[agent] || {};
  const enabled = (tool: ToolInfo) => selected[tool.name] !== false && (!tool.alias_of || selected[tool.alias_of] !== false);
  const visible = (catalog?.tools || []).filter((tool) => (!category || tool.category === category) && `${tool.name} ${tool.description}`.toLowerCase().includes(search.toLowerCase()));
  return <dialog ref={dialog} className="tools-dialog" aria-labelledby="tools-title" onCancel={(event) => { event.preventDefault(); if (!pending) close(); }}>
    <header><div><h2 id="tools-title" ref={heading} tabIndex={-1}><Icon name="tools" />Tools</h2><p>Choose which tools each agent can use in this workspace.</p></div>
      <button type="button" className="icon-button" aria-label="Close tools" disabled={pending} onClick={close}><Icon name="close" /></button></header>
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
    <div className="tools-list">{visible.map((tool) => <article className={`tool-setting${enabled(tool) ? " enabled" : ""}`} key={tool.name}>
      <div className="tool-setting-heading"><div><h3>{tool.name}</h3><span className="tool-category">{tool.category}</span>{tool.alias_of && <span className="tool-category">Alias of {tool.alias_of}</span>}</div>
        <label className="tool-switch"><span>{enabled(tool) ? "Enabled" : "Disabled"}</span><input type="checkbox" role="switch" aria-label={`Enable ${tool.name} for ${agent}`} checked={enabled(tool)} disabled={disabled || conflict || (!!tool.alias_of && selected[tool.alias_of] === false)} onChange={(event) => choose([tool.name], event.target.checked)} /></label></div>
      <p>{tool.description}</p>
      {profile?.mode === "reviewer" && !tool.reviewer_allowed && <p className="tool-restriction">This agent's read-only mode blocks execution of this tool.</p>}
      <details><summary>Tool schema · read-only</summary><pre>{JSON.stringify(tool.parameters, null, 2)}</pre></details>
    </article>)}{catalog && !visible.length && <p className="tools-empty">No tools match your search.</p>}</div>
    <footer><div><code>{catalog?.path || ".ngn/tools.yaml"}</code><small>{dirty ? "Unsaved changes" : "Workspace configuration"}</small></div>
      <button type="button" disabled={pending} onClick={() => void refresh()}>{conflict ? "Reload saved settings" : "Refresh"}</button>
      <button type="button" className="primary" disabled={disabled || conflict || !dirty} onClick={() => void save()}>Save tools</button></footer>
  </dialog>;
}
