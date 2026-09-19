import { useEffect, useRef, useState } from "react";
import { request } from "../../api/client";
import { Canvas } from "./Canvas";
import { Resources } from "./Resources";
import { ProviderSettings } from "./ProviderSettings";
import { NewDesignDialog } from "./NewDesignDialog";
import { ChannelRoutes } from "./ChannelRoutes";
import { TestPrompt } from "./TestPrompt";
import { Icon } from "../../components/Icon";
import { appendTrace, newAgent, removeAgent, traceMatches } from "./types";
import type { AgentDefinition, Design, RunSummary, TraceRecord, TraceReply, ChatMessage, ConversationReply } from "./types";
import "./designer.css";

export function Designer({ token, close, configureChannels }: { token: string; close: () => void; configureChannels: () => void }) {
  const [design, setDesign] = useState<Design>();
  const [newDesign, setNewDesign] = useState<Design>();
  const [toolDefinitions, setToolDefinitions] = useState<Record<string, unknown>>({});
  const [source, setSource] = useState("");
  const [yamlDirty, setYamlDirty] = useState(false);
  const [revision, setRevision] = useState("");
  const [selected, setSelected] = useState("");
  const [names, setNames] = useState<string[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [conversations, setConversations] = useState<RunSummary[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const conversation = useRef({ id: "", cursor: 0 });
  const [tools, setTools] = useState<string[]>([]);
  const [tab, setTab] = useState("canvas");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState(false);
  const [preview, setPreview] = useState<Record<string, unknown>>({});
  const [prompt, setPrompt] = useState("");
  const [runId, setRunId] = useState("");
  const [trace, setTrace] = useState<TraceRecord[]>([]);
  const [reply, setReply] = useState<TraceReply>();
  const [inspected, setInspected] = useState<TraceRecord>();
  const [filter, setFilter] = useState("steps");
  const [agentFilter, setAgentFilter] = useState("");
  const [continueRun, setContinueRun] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [resourceText, setResourceText] = useState("");
  const [demo, setDemo] = useState(false);
  const [debugOpen, setDebugOpen] = useState(false);
  const polling = useRef(0);
  const operating = useRef(false);
  const busy = pending || reply?.status === "running";

  async function api<T>(path: string, body?: object, method?: string): Promise<T> {
    return (await request(`designer${path}`, token, body, undefined, method)).json() as Promise<T>;
  }
  async function action(work: () => Promise<void>) {
    if (operating.current) return;
    operating.current = true;
    setPending(true); setError(""); setNotice("");
    try { await work(); } catch (cause) { setError(cause instanceof Error ? cause.message : "Designer operation failed"); }
    finally { operating.current = false; setPending(false); }
  }
  async function validate(text: string) {
    const result = await api<{ design: Design; preview: Record<string, unknown> }>("/validate", { source: text });
    setDesign(result.design); setPreview(result.preview); setSource(text); setYamlDirty(false);
    setSelected((id) => result.design.agents[id] ? id : result.design.entrypoint);
    return result.design;
  }
  async function refresh() {
    const catalog = await api<{ designs: string[]; starter: string; example: string; tools: string[]; tool_definitions: Record<string, unknown>; runs: RunSummary[]; conversations: RunSummary[]; demo: boolean }>("");
    setNames(catalog.designs); setTools(catalog.tools); setRuns(catalog.runs); setDemo(catalog.demo);
    setConversations(catalog.conversations);
    setToolDefinitions(catalog.tool_definitions);
    return catalog;
  }
  useEffect(() => { void action(async () => { const catalog = await refresh(); await validate(catalog.starter); if (catalog.conversations[0]) openRun(catalog.conversations[0].id); }); }, []);

  useEffect(() => {
    if (!runId) return;
    const generation = ++polling.current;
    let cursor = 0, disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await api<TraceReply>(`/runs/${runId}/${cursor}`);
        if (disposed || generation !== polling.current) return;
        setReply(result); setTrace((current) => appendTrace(current, result.events));
        if (result.channel_session) setContinueRun(false);
        if (conversation.current.id !== result.session_id) {
          conversation.current = { id: result.session_id, cursor: 0 }; setMessages([]);
        }
        let more = true;
        while (more) {
          const history = await api<ConversationReply>(`/conversations/${runId}/${conversation.current.cursor}`);
          if (disposed || generation !== polling.current) return;
          conversation.current.cursor = history.cursor;
          setMessages((current) => [...current, ...history.messages.filter((message) => !current.some((old) => old.id === message.id))]);
          more = history.more;
        }
        if (result.events.length) cursor = result.events.at(-1)!.sequence;
        if (result.status === "running" || result.events.length === 200) timer = setTimeout(() => void poll(), result.events.length === 200 ? 0 : 400);
        else void refresh();
      } catch (cause) {
        if (!disposed) { setError(cause instanceof Error ? cause.message : "Trace connection failed"); timer = setTimeout(() => void poll(), 1500); }
      }
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [runId, token]);

  function edit(next: Design) { setDesign(next); setPreview({}); }
  function editAgent(patch: Partial<AgentDefinition>) {
    if (design) edit({ ...design, agents: { ...design.agents, [selected]: { ...design.agents[selected], ...patch } } });
  }
  async function currentSource() {
    if (yamlDirty) { await validate(source); return source; }
    const result = await api<{ source: string }>("/serialize", design);
    setSource(result.source); return result.source;
  }
  function openRun(id: string, preserveChat = false) {
    polling.current++;
    if (!preserveChat) { setMessages([]); conversation.current = { id: "", cursor: 0 }; }
    setTrace([]); setReply(undefined); setInspected(undefined); setRunId(id); setContinueRun(!!id);
  }
  async function run() {
    const text = continueRun && runId ? "" : await currentSource();
    const result = await api<{ run_id: string }>("/run", { source: text, agent: selected, prompt, previous_run: continueRun ? runId : "" });
    openRun(result.run_id, continueRun); setContinueRun(true); setPrompt("");
  }
  const agent = design?.agents[selected];
  const providerId = agent?.provider || design?.defaults.provider || "";
  const provider = design?.providers[providerId];
  const runningAgents = new Set<string>();
  for (const record of trace) {
    const id = String(record.data.agent_id || "");
    if (record.kind === "agent_started") runningAgents.add(id);
    if (record.kind === "agent_finished") runningAgents.delete(id);
  }
  const visible = trace.filter((record) => traceMatches(record, filter) && (!agentFilter || record.data.agent_id === agentFilter));
  const inspectedData = inspected || visible.at(-1);
  const inspectedBody = inspectedData?.data.body;
  function formatBody(body: unknown): string {
    if (typeof body !== "string") return JSON.stringify(body, null, 2);
    try { return JSON.stringify(JSON.parse(body), null, 2); } catch { return body; }
  }

  return <section className="designer" aria-label="Agent Designer">
    <header className="designer-toolbar">
      <button className="designer-back" onClick={close} title="Back to chat" aria-label="Back to chat"><Icon name="chat" /></button>
       <div className="designer-brand"><span>ngn</span><span className="designer-divider">/</span><h1>Agent Designer</h1><small className="designer-experimental">Experimental</small></div>
      <select aria-label="Open saved design" value="" disabled={pending} onChange={(event) => void action(async () => {
        const result = await api<{ source: string; revision: string }>(`/designs/${event.target.value}`);
        await validate(result.source); setRevision(result.revision);
      })}><option value="">Open design…</option>{names.map((id) => <option key={id}>{id}</option>)}</select>
       <button disabled={busy} onClick={() => void action(async () => { const catalog = await refresh(); const initial = await api<{ design: Design }>("/validate", { source: catalog.starter }); setNewDesign(initial.design); })}>New</button>
      <button disabled={busy} onClick={() => void action(async () => { const catalog = await refresh(); await validate(catalog.example); setRevision(""); setPrompt("Ask analyst to calculate 17 × 23 and verify the result, then summarize its answer."); setContinueRun(false); setDebugOpen(true); setNotice("Example loaded. Send starts the selected provider and traces delegation."); })}>{demo ? "Example team" : "Live example"}</button>
      <span className="designer-toolbar-spacer" />
      <button disabled={!design || pending} onClick={() => void action(async () => { await validate(await currentSource()); setNotice("Definition is valid. Preview refreshed; no connections opened."); })}>Validate</button>
      <button className="designer-save" disabled={!design || pending} onClick={() => void action(async () => {
        const result = await api<{ revision: string }>("/save", { source: await currentSource(), revision });
        setRevision(result.revision); await refresh(); setNotice("Design saved.");
      })}>Save YAML</button>
      <button className="designer-test" aria-expanded={debugOpen} onClick={() => setDebugOpen(!debugOpen)}><Icon name="chat" size={15} />Test & inspect</button>
    </header>
    {error && <p role="alert" className="designer-error">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <nav className="designer-tabs" aria-label="Designer views">{["canvas", "yaml", "resources", "channels", "preview"].map((name) => <button key={name} aria-pressed={tab === name} onClick={() => void action(async () => {
      if (yamlDirty && name !== "yaml") await validate(source);
      if (name === "yaml") await currentSource();
      if (name === "resources" && design) setResourceText(JSON.stringify({ defaults: design.defaults, providers: design.providers, secrets: design.secrets, mcp_servers: design.mcp_servers }, null, 2));
      setTab(name);
    })}>{name}</button>)}<span className="designer-toolbar-spacer" /><span className="designer-mode">{demo ? "Offline demo" : "Live providers"} · {design ? Object.keys(design.agents).length : 0} agents{reply?.status === "running" ? " · Running" : ""}</span></nav>
    {design && <>
       <div className={`designer-body${debugOpen ? " with-chat" : ""}`}>
      <div className="designer-definition">
        <aside className="designer-agents" inert={yamlDirty}>
          <h2>Agents <span>{Object.keys(design.agents).length}</span></h2>{Object.entries(design.agents).map(([id, value]) => <button className="designer-agent-item" key={id} aria-pressed={id === selected} onClick={() => setSelected(id)}><Icon name="channels" size={15} /><span>{value.name || id}</span>{id === design.entrypoint && <small title="Entry agent">●</small>}</button>)}
          <details className="designer-add"><summary><Icon name="plus" size={14} /> Add agent</summary>
          <label>New agent ID<input value={draftName} onChange={(e) => setDraftName(e.target.value)} /></label>
          <button disabled={!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(draftName) || !!design.agents[draftName]} onClick={() => { edit({ ...design, agents: { ...design.agents, [draftName]: newAgent() } }); setSelected(draftName); setDraftName(""); }}>Add agent</button>
          </details><div className="designer-agent-actions"><button disabled={!agent} onClick={() => { let id = `${selected}-copy`; while (design.agents[id]) id += "2"; edit({ ...design, agents: { ...design.agents, [id]: structuredClone(agent!) } }); setSelected(id); }}>Copy</button>
          <button disabled={Object.keys(design.agents).length < 2} onClick={() => { const next = removeAgent(design, selected); edit(next); setSelected(next.entrypoint); }}>Remove</button></div>
        </aside>
        <div className="designer-workspace">
          {tab === "canvas" && <Canvas design={design} selected={selected} select={setSelected} change={edit} runningAgents={runningAgents} />}
          {tab === "channels" && <ChannelRoutes token={token} design={design} change={edit} configure={configureChannels} busy={busy} apply={() => action(async () => {
            const text = await currentSource();
            const saved = await api<{ revision: string }>("/save", { source: text, revision });
            setRevision(saved.revision); await refresh();
            await api("/channels", { source: text, revision: saved.revision });
            setNotice("Channel routes applied. New chats and /new use this version; existing conversations keep their pinned agent.");
          })} />}
          {tab === "yaml" && <><label>YAML definition<textarea className="designer-code" spellCheck={false} value={source} onChange={(e) => { setSource(e.target.value); setYamlDirty(true); }} /></label><button disabled={pending} onClick={() => void action(async () => { await validate(source); setNotice("YAML applied to canvas."); })}>Apply YAML</button><p>Visual edits produce canonical YAML when saved; YAML comments are retained only when saving directly from this editor.</p></>}
          {tab === "resources" && <><Resources design={design} change={(next) => { edit(next); setResourceText(JSON.stringify({ defaults: next.defaults, providers: next.providers, secrets: next.secrets, mcp_servers: next.mcp_servers }, null, 2)); }} /><details><summary>Advanced resource JSON</summary><p>MCP secrets map environment variable names to named secret references.</p>
            <textarea className="designer-code" aria-label="Shared resource configuration" spellCheck={false} value={resourceText} onChange={(e) => setResourceText(e.target.value)} />
            <button onClick={() => void action(async () => {
              const resources: Record<string, unknown> = JSON.parse(resourceText);
              if (Object.keys(resources).some((key) => !["defaults", "providers", "secrets", "mcp_servers"].includes(key))) throw new Error("Only shared resource fields are allowed here.");
              const result = await api<{ source: string }>("/serialize", { ...design, ...resources }); await validate(result.source); setNotice("Resources applied.");
            })}>Apply resources</button></details>
            {Object.keys(design.mcp_servers).map((id) => <button key={id} disabled={busy || demo} onClick={() => void action(async () => {
              const result = await api<{ tools: { name: string; description: string; parameters: unknown }[] }>("/discover", { source: await currentSource(), server: id });
              setPreview({ ...preview, [`MCP ${id}`]: result.tools }); setTab("preview"); setNotice(`Discovered ${result.tools.length} tools. Select them using the agent's MCP configuration.`);
            })}>Start {id} & discover tools</button>)}
          </>}
          {tab === "preview" && <><h2>Resolved configuration preview</h2><p>Validate to refresh. Actual per-call context and transport payloads appear in the execution inspector below.</p><pre>{JSON.stringify(preview, null, 2)}</pre></>}
        </div>
        {agent && <aside className="designer-inspector" inert={yamlDirty}><h2><Icon name="settings" size={15} />Agent settings<small>{selected}</small></h2>
          <label>Display name<input value={agent.name} onChange={(e) => editAgent({ name: e.target.value })} /></label>
          <label className="designer-check"><input type="checkbox" checked={design.entrypoint === selected} onChange={() => edit({ ...design, entrypoint: selected })} />Entry agent</label>
           <label>Provider<select value={agent.provider} onChange={(e) => editAgent({ provider: e.target.value })}><option value="">Default: {design.defaults.provider}</option>{Object.keys(design.providers).map((id) => <option key={id}>{id}</option>)}</select></label>
           {provider && <details><summary>Provider settings · {provider.type} · {provider.api}</summary>
             <p>Shared provider: {providerId}. Changes affect agents using this provider; existing conversations keep their pinned configuration.</p>
             <ProviderSettings provider={provider} secrets={Object.keys(design.secrets)} update={(patch) => edit({ ...design, providers: { ...design.providers, [providerId]: { ...provider, ...patch } } })} />
             {provider.secret && <p>Credential source: {design.secrets[provider.secret]?.source} · {design.secrets[provider.secret]?.name}</p>}
           </details>}
          <label>Instructions<textarea value={agent.instructions.text} onChange={(e) => editAgent({ instructions: { ...agent.instructions, text: e.target.value } })} /></label>
          <details><summary>Tools ({agent.tools.length})</summary>{tools.map((tool) => {
            const chosen = agent.tools.find((item) => item.ref.replace("builtin.", "") === tool);
            return <div key={tool}><label className="designer-check"><input type="checkbox" checked={!!chosen} onChange={(e) => editAgent({ tools: e.target.checked ? [...agent.tools, { ref: `builtin.${tool}`, description: "" }] : agent.tools.filter((item) => item !== chosen) })} />{tool}</label>
               {chosen && <details><summary>{tool} definition · read-only</summary><pre>{JSON.stringify(toolDefinitions[tool], null, 2)}</pre></details>}</div>;
          })}</details>
          <details><summary>Delegation targets ({agent.invokes.length})</summary>{Object.keys(design.agents).map((id) => {
            const edge = agent.invokes.find((item) => item.agent === id);
            return <div key={id}><label className="designer-check"><input type="checkbox" checked={!!edge} onChange={(e) => editAgent({ invokes: e.target.checked ? [...agent.invokes, { agent: id, description: "" }] : agent.invokes.filter((item) => item !== edge) })} />{id}</label>
              {edge && <textarea aria-label={`When to delegate to ${id}`} placeholder="When should this agent delegate here?" value={edge.description} onChange={(e) => editAgent({ invokes: agent.invokes.map((item) => item === edge ? { ...item, description: e.target.value } : item) })} />}</div>;
          })}</details>
          <details><summary>MCP tools ({agent.mcp.length} servers)</summary>{Object.keys(design.mcp_servers).map((id) => {
            const selection = agent.mcp.find((item) => item.server === id);
            return <div key={id}><label className="designer-check"><input type="checkbox" checked={!!selection} onChange={(e) => editAgent({ mcp: e.target.checked ? [...agent.mcp, { server: id, tools: [] }] : agent.mcp.filter((item) => item !== selection) })} />{id}</label>
              {selection && <label>Tool names (comma separated)<input value={selection.tools.map((item) => item.ref).join(", ")} onChange={(e) => editAgent({ mcp: agent.mcp.map((item) => item === selection ? { ...item, tools: e.target.value.split(",").map((name) => name.trim()).filter(Boolean).map((ref) => ({ ref, description: selection.tools.find((tool) => tool.ref === ref)?.description || "" })) } : item) })} /></label>}</div>;
           })}<p>Configure servers in Resources. MCP tool schemas and descriptions come from the server and are read-only; discover tools to inspect them in Preview.</p></details>
           <details><summary>Advanced</summary><label>Instruction file<input placeholder="agents/instructions.md" value={agent.instructions.file} onChange={(e) => editAgent({ instructions: { ...agent.instructions, file: e.target.value } })} /></label>
           {([ ["temperature", "Temperature", 0, 2], ["max_tokens", "Maximum output tokens", 1, undefined], ["top_p", "Top P", 0, 1] ] as const).map(([key, label, min, max]) => <label key={key}>{label}<input type="number" min={min} max={max} step={key === "max_tokens" ? 1 : 0.05} placeholder="Provider default" value={agent.generation?.[key] ?? ""} onChange={(e) => { const generation = { ...agent.generation }; if (e.target.value === "") delete generation[key]; else generation[key] = Number(e.target.value); editAgent({ generation }); }} /></label>)}
           <label>Stop sequences (one per line)<textarea value={agent.generation?.stop?.join("\n") || ""} onChange={(e) => { const generation = { ...agent.generation }; if (e.target.value) generation.stop = e.target.value.split("\n"); else delete generation.stop; editAgent({ generation }); }} /></label>
           <p>Blank generation fields use the provider default. Delegation depth is configured in Resources.</p>
          <label>Maximum model rounds<input type="number" min="1" max="1000" value={agent.max_tool_rounds} onChange={(e) => editAgent({ max_tool_rounds: Number(e.target.value) })} /></label></details>
        </aside>}
      </div>
      {debugOpen && <div className="designer-execution">
        <section className="designer-chat"><div className="designer-panel-heading"><h2><Icon name="chat" size={15} />{continueRun && reply ? reply.agent : selected}</h2><small>{reply ? `${reply.status} · ${reply.revision.slice(0, 8)}` : "New conversation"}</small></div>
            <select aria-label="Chat history" value={reply?.session_id || conversation.current.id} disabled={busy} onChange={(e) => { if (!e.target.value) { openRun(""); setPrompt(""); return; } const chat = conversations.find((item) => item.session_id === e.target.value); if (chat) openRun(chat.id); }}><option value="">＋ New chat</option>{conversations.map((chat) => <option key={chat.session_id} value={chat.session_id}>{chat.agent} · {chat.created.slice(0, 19)}</option>)}{reply && !conversations.some((chat) => chat.session_id === reply.session_id) && <option value={reply.session_id}>Current conversation</option>}</select>
           <select aria-label="Execution history" value={runId} disabled={busy} onChange={(e) => openRun(e.target.value)}><option value="">Inspect execution…</option>{runs.filter((run) => !reply || run.session_id === reply.session_id).map((run) => <option key={run.id} value={run.id}>{run.agent} · {run.status} · {run.created.slice(0, 19)}</option>)}{runId && !runs.some((run) => run.id === runId) && <option value={runId}>Current run</option>}</select>
           <div className="designer-transcript" aria-live="polite">{messages.map((message) => <article key={message.id}><strong>{message.role === "user" ? "You" : reply?.agent || selected}</strong><p>{message.text}</p></article>)}</div>
          {reply?.approval?.approval_id && <div className="designer-approval"><h3>Approval requested</h3><p>{reply.approval.description}</p><pre>{reply.approval.preview}</pre>{["allow", "deny"].map((decision) => <button key={decision} disabled={pending} onClick={() => void action(async () => {
            await request("approval", token, { run_id: runId, approval_id: reply.approval.approval_id, call_id: reply.approval.id, decision });
          })}>{decision}</button>)}</div>}
          <label className="designer-prompt"><span className="sr-only">Message</span><TestPrompt target={continueRun && reply ? reply.agent : selected} value={prompt} change={setPrompt} canSend={!busy} send={() => void action(run)} /></label>
           <div className="designer-chat-actions">{runId && <label className="designer-check" title={reply?.channel_session ? "Continue in the bound web session or channel chat" : "Continue this conversation using its pinned agent definition"}><input type="checkbox" disabled={reply?.channel_session} checked={continueRun} onChange={(e) => setContinueRun(e.target.checked)} />{reply?.channel_session ? "Channel conversation" : "Continue"}</label>}
          <button className="designer-send" title="Ctrl+Enter or ⌘+Enter" disabled={busy || !prompt.trim()} onClick={() => void action(run)}>Send</button>
          <button disabled={reply?.status !== "running"} onClick={() => void action(async () => { await request("cancel", token, { run_id: runId }); })}>Cancel run</button>
          <button title="Delete trace" aria-label="Delete trace" disabled={!runId || busy} onClick={() => void action(async () => { await api(`/runs/${runId}`, {}, "DELETE"); setRunId(""); setTrace([]); setReply(undefined); await refresh(); })}><Icon name="trash" size={14} /></button></div>
        </section>
        <section className="designer-trace"><div className="designer-panel-heading"><h2>Execution inspector <small>{trace.length} events</small></h2><button aria-label="Close execution inspector" title="Close inspector" onClick={() => setDebugOpen(false)}><Icon name="close" size={14} /></button></div>
          <div className="designer-tabs"><select aria-label="Trace category" value={filter} onChange={(e) => { setFilter(e.target.value); setInspected(undefined); }}><option value="steps">Execution steps</option><option value="">All raw events</option>{Object.entries({ model_context: "Model context", http_request: "API requests", http_response: "API responses", http_stream: "Raw response stream", tool_: "Tools", delegation: "Delegation", error: "Errors" }).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
            <select aria-label="Trace agent" value={agentFilter} onChange={(e) => setAgentFilter(e.target.value)}><option value="">All agents</option>{Object.keys(design.agents).map((id) => <option key={id}>{id}</option>)}</select></div>
          <div className="designer-trace-columns"><ol>{visible.map((record) => <li key={record.sequence}><button aria-pressed={record.sequence === inspectedData?.sequence} onClick={() => setInspected(record)}><small>{record.sequence} · {String(record.data.agent_id || "run")}</small>{record.kind.replaceAll("_", " ")}</button></li>)}</ol>
            <div className="designer-record">{inspectedBody !== undefined && <><h3>Wire payload</h3><pre tabIndex={0}>{formatBody(inspectedBody)}</pre><details><summary>Event metadata</summary><pre>{JSON.stringify(inspectedData, null, 2)}</pre></details></>}{inspectedBody === undefined && <pre tabIndex={0}>{inspectedData ? JSON.stringify(inspectedData, null, 2) : "Send a message to inspect context, requests, tools and delegated tasks."}</pre>}</div></div>
        </section>
       </div>}
       </div>
       <footer className="designer-statusbar"><span>{design.id} · {revision ? "Saved definition" : "Draft"}</span><span>{Object.values(design.agents).reduce((count, item) => count + item.invokes.length, 0)} delegation links</span><button onClick={() => setDebugOpen(!debugOpen)}>{debugOpen ? "Hide test panel" : "Open test chat & inspector"}</button></footer>
     </>}
     {newDesign && <NewDesignDialog template={newDesign} names={names} close={() => setNewDesign(undefined)} create={async (next) => {
       const encoded = await api<{ source: string }>("/serialize", next);
       const validated = await api<{ design: Design; preview: Record<string, unknown> }>("/validate", { source: encoded.source });
       const saved = await api<{ revision: string }>("/save", { source: encoded.source, revision: "" });
       setDesign(validated.design); setPreview(validated.preview); setSource(encoded.source); setYamlDirty(false);
       setRevision(saved.revision); setSelected(next.entrypoint); setTab("canvas"); openRun(""); setPrompt("");
       setNames((current) => [...new Set([...current, next.id])].sort()); setNewDesign(undefined); setNotice(`Created ${next.id}.`);
     }} />}
   </section>;
}
