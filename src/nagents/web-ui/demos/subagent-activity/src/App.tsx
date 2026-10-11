import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { advanceTo, nextEventTime, queuedCount, queueMessage, startDemo } from "./engine";
import { DEMO_END, definitions } from "./fixtures";
import type { ActivityState, AgentId, AgentRun, RecordItem, Status, ToolCall } from "./types";

type Design = "tree" | "workspace" | "board";
const designs: { id: Design; letter: string; name: string; description: string; detail: string }[] = [
  { id: "tree", letter: "A", name: "Conversation tree", description: "Work unfolds inside the chat.", detail: "Expand a branch to see its work and send a note. Parent and child relationships stay together in the conversation." },
  { id: "workspace", letter: "B", name: "Agent workspace", description: "One agent, room to investigate.", detail: "Choose an agent on the left. Its activity gets a full workspace, while the parent conversation stays in view." },
  { id: "board", letter: "C", name: "Activity board", description: "Scan the team. Open the detail.", detail: "Every task keeps its current action visible. A persistent lower panel lets you inspect and message one agent without losing the team." },
];

function Glyph({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, string> = {
    branch: "M5 3v11a3 3 0 0 0 3 3h8M5 8h5a5 5 0 0 0 5-5M13 15l3 2-3 2",
    chat: "M4 3h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H8l-5 3V5a2 2 0 0 1 1-2Z",
    arrow: "M4 10h12m-5-5 5 5-5 5",
    chevron: "m7 4 6 6-6 6",
    check: "m4 10 4 4 8-8",
    clock: "M10 5v5l3 2M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
    play: "m6 3 10 7-10 7Z",
    pause: "M7 3v14M13 3v14",
    reset: "M4 7a7 7 0 1 1-1 7M4 2v5H0",
    next: "m3 4 8 6-8 6ZM15 4v12",
    tool: "m7 5-5 5 5 5m6-10 5 5-5 5m-2-13-2 16",
    alert: "M10 5v6m0 3v.1M10 1 1 18h18Z",
    inbox: "M3 4h14l2 10v3H1v-3ZM1 13h5l2 3h4l2-3h5",
    send: "m3 3 15 7-15 7 3-7Zm3 7h12",
    grid: "M2 2h6v6H2ZM12 2h6v6h-6ZM2 12h6v6H2ZM12 12h6v6h-6Z",
    focus: "M7 2H2v5M13 2h5v5M18 13v5h-5M7 18H2v-5",
  };
  return <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] ?? paths.branch} /></svg>;
}

function time(seconds: number) { return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`; }
function duration(agent: AgentRun, now: number) { return `${Math.max(0, (agent.finishedAt ?? now) - agent.startedAt)}s`; }
const statusNames: Record<Status, string> = { running: "Running", waiting: "Needs approval", failed: "Failed check", completed: "Complete" };

function Badge({ status }: { status: Status }) {
  return <span className={`status status-${status}`}><span className="status-dot" />{statusNames[status]}</span>;
}

function Avatar({ agent, small = false }: { agent: AgentRun; small?: boolean }) {
  return <span className={`agent-avatar tone-${agent.definition.color}${small ? " small" : ""}`} aria-hidden="true">{agent.definition.initials}</span>;
}

function Metadata({ agent }: { agent: AgentRun }) {
  return <span className="agent-model"><span className="provider-mark" aria-hidden="true">◎</span>{agent.definition.provider}<span className="meta-separator">/</span>{agent.definition.model}</span>;
}

function RunningBars() { return <span className="running-bars" aria-hidden="true"><i /><i /><i /><i /></span>; }

function latestTool(agent: AgentRun): ToolCall | undefined {
  for (let i = agent.records.length - 1; i >= 0; i--) {
    const record = agent.records[i];
    if (record.kind === "tool") return record.tool;
  }
  return undefined;
}

function ToolEvidence({ tool, now }: { tool: ToolCall; now: number }) {
  const id = useId();
  const running = tool.status === "running";
  const tone = tool.status === "failed" ? "failed" : tool.status === "waiting" ? "waiting" : tool.status;
  let output = tool.output;
  try { output = JSON.stringify(JSON.parse(tool.output), null, 2); } catch { /* Text and shell output remain literal. */ }
  return <div className={`tool-evidence evidence-${tone}`}>
    <div className="tool-evidence-heading"><span className="tool-name"><Glyph name="tool" size={14} /><code>{tool.name}</code></span><span className="tool-time">{running && <RunningBars />}{tool.status === "waiting" ? "Not started" : `${(tool.finishedAt ?? now) - tool.startedAt}s`}</span></div>
    <strong>{tool.title}</strong>
    <p className="tool-summary">{tool.summary}</p>
    {running && <div className="activity-skeleton" aria-hidden="true"><i /><i /></div>}
    <details className="tool-disclosure"><summary>Arguments &amp; {running ? "output so far" : "result"}<Glyph name="chevron" size={13} /></summary>
      <div className="evidence-content"><h5 id={`${id}-args`}>Arguments</h5><pre aria-labelledby={`${id}-args`}>{JSON.stringify(tool.arguments, null, 2)}</pre><h5 id={`${id}-result`}>{running ? "Output so far" : "Result"}</h5><pre className={tool.format === "shell" ? "shell-output" : ""} aria-labelledby={`${id}-result`}>{output || "The command has not started. No output yet."}</pre></div>
    </details>
  </div>;
}

function TranscriptRecord({ record, agent, now }: { record: RecordItem; agent: AgentRun; now: number }) {
  if (record.kind === "tool") return <li className="record record-tool"><span className="record-marker"><Glyph name="tool" size={13} /></span><ToolEvidence tool={record.tool} now={now} /></li>;
  const message = record.message;
  return <li className={`record record-message message-${message.role}`}>
    <span className="record-marker">{message.role === "user" ? <Glyph name="chat" size={12} /> : <span className={`mini-dot tone-${agent.definition.color}`} />}</span>
    <div className="message-content"><div className="message-byline"><strong>{message.role === "user" ? "You → " + agent.definition.name : agent.definition.name}</strong><time>{time(message.at)}</time></div><p>{message.text}</p>
      {message.delivery && <span className={`delivery delivery-${message.delivery}`}><Glyph name={message.delivery === "queued" ? "clock" : "check"} size={12} />{message.delivery === "queued" ? "Queued · next response boundary" : `Picked up at ${time(message.pickedUpAt ?? message.at)}`}</span>}
    </div>
  </li>;
}

function MessageComposer({ agent, onSend, playing }: { agent: AgentRun; onSend: (id: AgentId, value: string) => void; playing: boolean }) {
  const [draft, setDraft] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);
  const id = useId();
  const queued = queuedCount(agent);
  const send = (event?: FormEvent) => {
    event?.preventDefault();
    if (!draft.trim()) return;
    onSend(agent.definition.id, draft);
    setDraft("");
    textarea.current?.focus();
  };
  return <form className="agent-composer" onSubmit={send}>
    <label htmlFor={id}><Glyph name="chat" size={14} />Message {agent.definition.name}<span>Only this agent</span></label>
    <div className="compose-input"><textarea id={id} ref={textarea} value={draft} onChange={event => setDraft(event.target.value)} placeholder={`A note for ${agent.definition.name}…`} rows={2} maxLength={4000} aria-describedby={`${id}-hint`} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }} /><button type="submit" className="send-note" disabled={!draft.trim()} aria-label={`Send message to ${agent.definition.name}`}><Glyph name="send" size={17} /></button></div>
    <p id={`${id}-hint`} className="compose-hint">{queued > 0 ? <><span className="queued-pill">{queued} queued</span>{playing ? "Picked up at the next response boundary." : "Demo paused. Play or advance to see delivery."}</> : <>Notes enter the next response. A running tool keeps working.</>}</p>
  </form>;
}

interface InspectorProps { agent: AgentRun; state: ActivityState; onSend: (id: AgentId, text: string) => void; playing: boolean; compact?: boolean; board?: boolean }
function AgentInspector({ agent, state, onSend, playing, compact = false, board = false }: InspectorProps) {
  const [filter, setFilter] = useState<"all" | "messages" | "tools">("all");
  const records = agent.records.filter(record => filter === "all" || (filter === "tools" ? record.kind === "tool" : record.kind === "message"));
  const parent = state.agents[agent.definition.parentId]?.definition.name ?? "Assistant";
  return <section className={`agent-inspector${compact ? " inspector-compact" : ""}${board ? " inspector-board" : ""}`} aria-label={`${agent.definition.name} activity and messages`}>
    {!compact && <header className="inspector-header"><div className="inspector-title"><Avatar agent={agent} /><div><div className="name-line"><h2>{agent.definition.name}</h2><Badge status={agent.status} /></div><Metadata agent={agent} /></div><span className="elapsed"><Glyph name="clock" size={13} />{duration(agent, state.now)}</span></div><div className="ownership"><Glyph name="branch" size={13} />Assistant{parent !== "Assistant" && <><Glyph name="chevron" size={11} />{parent}</>}<Glyph name="chevron" size={11} /><strong>{agent.definition.name}</strong><span>Reports to {parent}</span></div></header>}
    <div className="inspector-body"><div className="activity-column">
      <div className={`current-action action-${agent.status}`}><span>{agent.status === "running" ? <RunningBars /> : <Glyph name={agent.status === "completed" ? "check" : "alert"} size={16} />}</span><div><span className="eyebrow">{agent.status === "completed" ? "Result" : "Current activity"}</span><p>{agent.activity}</p></div></div>
      <details className="assignment"><summary>Assigned task<Glyph name="chevron" size={12} /></summary><p>{agent.definition.assignment}</p></details>
      <div className="activity-toolbar"><span className="eyebrow">Activity &amp; messages</span><div className="segment-control" aria-label="Filter agent activity">{(["all", "messages", "tools"] as const).map(item => <button key={item} type="button" aria-pressed={filter === item} onClick={() => setFilter(item)}>{item === "all" ? "All" : item === "messages" ? "Messages" : "Tools"}</button>)}</div></div>
      <ol className="transcript">{records.map(record => <TranscriptRecord key={record.kind === "tool" ? record.tool.id : record.message.id} record={record} agent={agent} now={state.now} />)}{!records.length && <li className="empty-records">No {filter} events yet.</li>}</ol>
    </div><div className="compose-column"><MessageComposer key={agent.definition.id} agent={agent} onSend={onSend} playing={playing} />{board && <div className="delivery-explainer"><Glyph name="inbox" size={19} /><strong>A note, without an interruption</strong><p>Your message joins this agent’s next response. Tool execution continues, and the thread shows when your note is picked up.</p></div>}</div></div>
  </section>;
}

function ParentConversation({ state, children, compact = false }: { state: ActivityState; children?: ReactNode; compact?: boolean }) {
  const done = Object.values(state.agents).filter(agent => agent.status === "completed").length;
  return <section className={`parent-conversation${compact ? " parent-compact" : ""}`} aria-label="Parent conversation">
    <header className="conversation-heading"><div className="ngn-avatar">n</div><div><strong>Assistant</strong><span>gpt-6.1-sol · main conversation</span></div><span className="conversation-label">Parent</span></header>
    <div className="parent-message user-bubble"><span>You</span><p>Show images directly in chat, with a download action. Make sure it works on mobile.</p></div>
    <div className="parent-message"><span>Assistant</span><p>I’ve split the work across research, implementation, and verification. You can follow each branch and send a note directly to an agent.</p></div>
    {children}
    {done > 0 && <div className="parent-result"><Glyph name="inbox" size={16} /><div><strong>{done} of 4 agents returned results</strong><p>{done === 4 ? "The image-card update is ready for the parent’s review." : "Results come back to the agent that delegated the task."}</p></div></div>}
  </section>;
}

interface ViewProps { state: ActivityState; selected: AgentId; onSelect: (id: AgentId) => void; onSend: (id: AgentId, text: string) => void; playing: boolean }

function TreeView(props: ViewProps) {
  const { state, selected, onSelect } = props;
  const [expanded, setExpanded] = useState<Record<string, boolean>>({ research: true });
  function branch(agent: AgentRun, depth = 0): ReactNode {
    const definition = agent.definition;
    const children = Object.values(state.agents).filter(child => child.definition.parentId === definition.id);
    const isExpanded = expanded[definition.id] ?? false;
    const queue = queuedCount(agent);
    return <li key={definition.id} className="tree-branch"><article className={`tree-task${isExpanded ? " is-expanded" : ""}${selected === definition.id ? " is-selected" : ""}`}>
      <button type="button" className="tree-task-summary" aria-expanded={isExpanded} aria-controls={`tree-panel-${definition.id}`} onClick={() => { setExpanded(current => ({ ...current, [definition.id]: !isExpanded })); onSelect(definition.id); }}>
        <span className={`expand-chevron${isExpanded ? " open" : ""}`}><Glyph name="chevron" size={14} /></span><Avatar agent={agent} /><span className="tree-task-copy"><span className="name-line"><strong>{definition.name}</strong><span className="role-name">{definition.role}</span></span><span className="tree-current">{agent.status === "running" && <RunningBars />}{agent.activity}</span><Metadata agent={agent} /></span><span className="tree-task-end"><Badge status={agent.status} /><span className="elapsed">{duration(agent, state.now)}{queue > 0 && <span className="queued-pill">{queue} queued</span>}</span></span>
      </button>
      <div id={`tree-panel-${definition.id}`} hidden={!isExpanded}>{isExpanded && <AgentInspector agent={agent} state={state} onSend={props.onSend} playing={props.playing} compact />}</div>
    </article>{children.length > 0 && <div className="tree-child-label"><Glyph name="branch" size={12} />Delegated by {definition.name}</div>}{children.length > 0 && <ul className="task-tree nested">{children.map(child => branch(child, depth + 1))}</ul>}</li>;
  }
  return <div className="tree-view"><div className="conversation-context"><span className="eyebrow">Workspace / Image experience</span><span className="subtle">4 agents · 2 levels</span></div><ParentConversation state={state}><div className="tree-group-heading"><span><Glyph name="branch" size={16} />Delegated work</span><span>Choose a branch to inspect or message</span></div><ul className="task-tree">{Object.values(state.agents).filter(agent => agent.definition.parentId === "assistant").map(agent => branch(agent))}</ul></ParentConversation><div className="parent-compose-placeholder"><Glyph name="chat" size={16} /><span>Main conversation · demo read-only</span><span>Agent threads above are interactive</span></div></div>;
}

function AgentNav({ agent, state, selected, onSelect }: { agent: AgentRun; state: ActivityState; selected: AgentId; onSelect: (id: AgentId) => void }) {
  const nested = agent.definition.parentId !== "assistant";
  return <button type="button" className={`agent-nav-item${selected === agent.definition.id ? " selected" : ""}${nested ? " nested-nav" : ""}`} aria-pressed={selected === agent.definition.id} onClick={() => onSelect(agent.definition.id)}><div className="nav-agent-line"><Avatar agent={agent} small /><strong>{agent.definition.name}</strong><span className={`nav-state state-${agent.status}`} aria-label={statusNames[agent.status]} /></div><p>{agent.activity}</p><span className="nav-meta">{nested ? `↳ ${state.agents[agent.definition.parentId].definition.name}` : "↳ Assistant"}<span>{duration(agent, state.now)}{queuedCount(agent) > 0 && ` · ${queuedCount(agent)} queued`}</span></span></button>;
}

function WorkspaceView(props: ViewProps) {
  const { state, selected, onSelect } = props;
  return <div className="workspace-view"><aside className="agent-navigation" aria-label="Delegation tree"><div className="rail-heading"><span className="eyebrow">Working team</span><span>4</span></div><div className="nav-parent"><span className="ngn-avatar">n</span><span>Assistant<small>Parent conversation</small></span><Glyph name="branch" size={16} /></div><nav>{definitions.map(definition => <AgentNav key={definition.id} agent={state.agents[definition.id]} state={state} selected={selected} onSelect={onSelect} />)}</nav><div className="rail-footnote"><Glyph name="branch" size={15} /><p>Layout reports to Builder. The other agents report to Assistant.</p></div></aside><AgentInspector key={selected} agent={state.agents[selected]} state={state} onSend={props.onSend} playing={props.playing} /><aside className="parent-rail"><div className="rail-heading"><span className="eyebrow">Main conversation</span><Glyph name="chat" size={16} /></div><ParentConversation state={state} compact /><div className="parent-rail-note"><span className="tiny-label">What you can inspect</span><p>Published messages, tool calls, arguments, results, and state changes.</p><p className="subtle">This view shows emitted activity. It does not expose private model reasoning.</p></div></aside></div>;
}

function AgentCard({ agent, state, selected, onSelect }: { agent: AgentRun; state: ActivityState; selected: AgentId; onSelect: (id: AgentId) => void }) {
  const tool = latestTool(agent);
  const parent = state.agents[agent.definition.parentId]?.definition.name ?? "Assistant";
  return <article className={`agent-card tone-${agent.definition.color}${selected === agent.definition.id ? " selected" : ""}`}><header><Avatar agent={agent} /><div><h3>{agent.definition.name}</h3><span>{agent.definition.role}</span></div><span className="elapsed">{duration(agent, state.now)}</span></header><Metadata agent={agent} /><div className="card-status"><Badge status={agent.status} />{queuedCount(agent) > 0 && <span className="queued-pill">{queuedCount(agent)} queued</span>}</div><p className="card-current">{agent.activity}</p><div className="card-evidence">{tool ? <><span><Glyph name="tool" size={13} /><code>{tool.name}</code>{tool.status === "running" && <RunningBars />}</span><p>{tool.title}</p></> : <><span><Glyph name="check" size={13} />Result returned</span><p>44 px touch targets · filename wraps</p></>}</div><footer><span><Glyph name="branch" size={12} />{parent}</span><button type="button" className="card-open" aria-label={`Inspect and message ${agent.definition.name}`} aria-pressed={selected === agent.definition.id} onClick={() => onSelect(agent.definition.id)}>{selected === agent.definition.id ? "Inspecting" : "Open thread"}<Glyph name="arrow" size={14} /></button></footer></article>;
}

function BoardView(props: ViewProps) {
  const { state, selected, onSelect } = props;
  const [attentionOnly, setAttentionOnly] = useState(false);
  const agents = Object.values(state.agents);
  const visible = agents.filter(agent => !attentionOnly || agent.status === "waiting" || agent.status === "failed");
  return <div className="board-view"><div className="board-conversation"><span className="ngn-avatar">n</span><div><span className="eyebrow">Assistant / Image experience</span><p>Show images directly in chat, with a download action.</p></div><span className="team-count"><strong>{agents.filter(agent => agent.status === "completed").length}</strong> / 4 returned</span></div><div className="board-controls"><span className="eyebrow">Team activity</span><div className="segment-control"><button type="button" aria-pressed={!attentionOnly} onClick={() => setAttentionOnly(false)}>All agents</button><button type="button" aria-pressed={attentionOnly} onClick={() => setAttentionOnly(true)}>Needs attention <span>{agents.filter(agent => agent.status === "waiting" || agent.status === "failed").length}</span></button></div><span className="subtle board-relationship"><Glyph name="branch" size={14} />Assistant → Explorer, Builder, Verifier · Builder → Layout</span></div><div className="agent-board">{visible.map(agent => <AgentCard key={agent.definition.id} agent={agent} state={state} selected={selected} onSelect={onSelect} />)}{visible.length === 0 && <p className="board-empty"><Glyph name="check" size={19} />No agents need attention.</p>}</div><section className="activity-dock" aria-label="Selected agent detail"><div className="dock-heading"><span><Glyph name="focus" size={14} />Focused thread</span><span>The team stays visible above</span></div><AgentInspector key={selected} agent={state.agents[selected]} state={state} onSend={props.onSend} playing={props.playing} board /></section></div>;
}

export function App() {
  const hash = window.location.hash.slice(1);
  const [design, setDesign] = useState<Design>(designs.some(item => item.id === hash) ? hash as Design : "tree");
  const [state, setState] = useState(startDemo);
  const [selected, setSelected] = useState<AgentId>("research");
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const active = designs.find(item => item.id === design)!;
  const ended = state.now >= DEMO_END && Object.keys(state.followUps).length === 0;

  useEffect(() => {
    if (!playing || ended) return;
    const timer = window.setInterval(() => setState(current => advanceTo(current, current.now + 1)), 1000 / speed);
    return () => window.clearInterval(timer);
  }, [playing, speed, ended]);
  useEffect(() => { if (ended) setPlaying(false); }, [ended]);

  const onSend = (id: AgentId, value: string) => setState(current => queueMessage(current, id, value));
  const viewProps = { state, selected, onSelect: setSelected, onSend, playing };
  return <div className={`studio design-${design}${playing ? " is-playing" : " is-paused"}`}><a className="skip-link" href="#design-preview">Skip to interactive design</a><header className="studio-header"><div className="studio-brand"><span className="brand-mark">n<span /></span><div><span className="eyebrow">ngn / interaction studies</span><h1>Inside the team</h1></div></div><div className="demo-label"><span />Interactive demo<span className="demo-label-note">No live agents or backend</span></div></header>
    <div className="design-switcher" aria-label="Choose a design direction">{designs.map(item => <button type="button" key={item.id} className={`design-choice${design === item.id ? " active" : ""}`} aria-pressed={design === item.id} onClick={() => { setDesign(item.id); window.history.replaceState(null, "", `#${item.id}`); }}><span className="option-letter">{item.letter}</span><span><strong>{item.name}</strong><small>{item.description}</small></span><Glyph name={item.id === "tree" ? "branch" : item.id === "workspace" ? "focus" : "grid"} size={20} /></button>)}</div>
    <section className="simulation-controls" aria-label="Simulated event timeline"><div className="transport-buttons"><button type="button" className="play-button" onClick={() => { if (ended) setState(startDemo()); setPlaying(value => !value); }}><Glyph name={playing ? "pause" : "play"} size={15} />{playing ? "Pause" : ended ? "Replay" : "Play demo"}</button><button type="button" title="Advance to the next event" onClick={() => { setPlaying(false); setState(current => advanceTo(current, nextEventTime(current))); }}><Glyph name="next" size={15} /><span>Next event</span></button><button type="button" className="reset-button" aria-label="Reset demo and clear messages" onClick={() => { setState(startDemo()); setPlaying(false); }}><Glyph name="reset" size={16} /></button></div><div className="timeline-progress"><div><span>{playing ? "Simulated activity playing" : ended ? "Demo complete" : "Timeline paused"}</span><time>{time(state.now)} <span>/ {time(DEMO_END)}</span></time></div><progress value={Math.min(state.now, DEMO_END)} max={DEMO_END} aria-label="Demo timeline" /></div><label className="speed-control">Speed<select value={speed} onChange={event => setSpeed(Number(event.target.value))}><option value={1}>1×</option><option value={2}>2×</option><option value={4}>4×</option></select></label></section>
    <div className="design-caption"><span className="option-letter">{active.letter}</span><p>{active.detail}</p><span className="demo-tag">Simulated data</span></div>
    <main id="design-preview" tabIndex={-1}>{design === "tree" ? <TreeView {...viewProps} /> : design === "workspace" ? <WorkspaceView {...viewProps} /> : <BoardView {...viewProps} />}</main><footer className="studio-footer"><span>Design study · messages and tools are illustrative</span><span>Try: send Explorer a note, then advance to 0:13.</span></footer><p className="sr-only" role="status" aria-live="polite">{state.announcement}</p></div>;
}
