import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent, ReactNode, RefObject } from "react";
import { advanceTo, nextEventTime, queuedCount, queueMessage, startDemo } from "./engine";
import { DEMO_END, definitions } from "./fixtures";
import type { ActivityState, AgentId, AgentRun, RecordItem, Status, ToolCall } from "./types";

type Design = "tree" | "workspace" | "board";
const designs: { id: Design; letter: string; name: string; description: string; detail: string }[] = [
  { id: "tree", letter: "A", name: "Tree + team index", description: "Expand the work where it happened.", detail: "Conversation branches hold the full activity and composer. A slim team index on the right jumps to any branch; several can stay open." },
  { id: "workspace", letter: "B", name: "Tree + side inspector", description: "Keep the conversation in view.", detail: "The chat keeps a compact tree and activity excerpts. Inspect tools and message an agent in the right panel, without expanding the main conversation." },
  { id: "board", letter: "C", name: "Tree + focus rail", description: "Give one branch room to work.", detail: "A small tree opens one full-width thread inside the conversation. The right rail collapses to agent avatars; expand it whenever you need the team overview." },
];

function Glyph({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, string> = {
    branch: "M5 3v11a3 3 0 0 0 3 3h8M5 8h5a5 5 0 0 0 5-5M13 15l3 2-3 2",
    chat: "M4 3h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H8l-5 3V5a2 2 0 0 1 1-2Z",
    arrow: "M4 10h12m-5-5 5 5-5 5", chevron: "m7 4 6 6-6 6", check: "m4 10 4 4 8-8",
    clock: "M10 5v5l3 2M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
    play: "m6 3 10 7-10 7Z", pause: "M7 3v14M13 3v14", reset: "M4 7a7 7 0 1 1-1 7M4 2v5H0", next: "m3 4 8 6-8 6ZM15 4v12",
    tool: "m7 5-5 5 5 5m6-10 5 5-5 5m-2-13-2 16", alert: "M10 5v6m0 3v.1M10 1 1 18h18Z",
    inbox: "M3 4h14l2 10v3H1v-3ZM1 13h5l2 3h4l2-3h5", send: "m3 3 15 7-15 7 3-7Zm3 7h12",
    panel: "M2 3h16v14H2ZM12 3v14", focus: "M7 2H2v5M13 2h5v5M18 13v5h-5M7 18H2v-5",
    close: "m5 5 10 10M15 5 5 15", back: "M16 10H4m5-5-5 5 5 5", collapse: "m8 5 5 5-5 5M17 3v14",
    attach: "m7 10 5-5a2.5 2.5 0 0 1 3.5 3.5l-6 6a4 4 0 0 1-5.7-5.7l6-6M6.5 11.5l5-5",
    wave: "M2 8v4M6 5v10M10 2v16M14 5v10M18 8v4",
  };
  return <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] ?? paths.branch} /></svg>;
}

function time(seconds: number) { return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`; }
function duration(agent: AgentRun, now: number) { return `${Math.max(0, (agent.finishedAt ?? now) - agent.startedAt)}s`; }
const statusNames: Record<Status, string> = { running: "Running", waiting: "Needs approval", failed: "Failed check", completed: "Complete" };
function Badge({ status }: { status: Status }) { return <span className={`status status-${status}`}><span className="status-dot" />{statusNames[status]}</span>; }
function Avatar({ agent, small = false }: { agent: AgentRun; small?: boolean }) { return <span className={`agent-avatar tone-${agent.definition.color}${small ? " small" : ""}`} aria-hidden="true">{agent.definition.initials}</span>; }
function Metadata({ agent }: { agent: AgentRun }) { return <span className="agent-model">{agent.definition.provider}<span className="meta-separator">/</span>{agent.definition.model}</span>; }
function RunningBars() { return <span className="running-bars" aria-hidden="true"><i /><i /><i /><i /></span>; }
function latestTool(agent: AgentRun): ToolCall | undefined { return agent.records.findLast(record => record.kind === "tool")?.tool; }
function latestMessage(agent: AgentRun): string { const record = agent.records.findLast(record => record.kind === "message" && record.message.role === "assistant"); return record?.kind === "message" ? record.message.text : agent.activity; }

function ToolEvidence({ tool, now }: { tool: ToolCall; now: number }) {
  const id = useId();
  const running = tool.status === "running";
  let output = tool.output;
  try { output = JSON.stringify(JSON.parse(tool.output), null, 2); } catch { /* Literal text and shell output. */ }
  return <div className={`tool-evidence evidence-${tool.status}`}>
    <div className="tool-evidence-heading"><span className="tool-name"><Glyph name="tool" size={14} /><code>{tool.name}</code></span><span className="tool-time">{running && <RunningBars />}{tool.status === "waiting" ? "Not started" : `${(tool.finishedAt ?? now) - tool.startedAt}s`}</span></div>
    <strong>{tool.title}</strong><p className="tool-summary">{tool.summary}</p>
    {running && <div className="activity-skeleton" aria-hidden="true"><i /><i /></div>}
    <details className="tool-disclosure"><summary>Arguments &amp; {running ? "output so far" : "result"}<Glyph name="chevron" size={13} /></summary><div className="evidence-content"><h5 id={`${id}-args`}>Arguments</h5><pre aria-labelledby={`${id}-args`}>{JSON.stringify(tool.arguments, null, 2)}</pre><h5 id={`${id}-result`}>{running ? "Output so far" : "Result"}</h5><pre className={tool.format === "shell" ? "shell-output" : ""} aria-labelledby={`${id}-result`}>{output || "The command has not started. No output yet."}</pre></div></details>
  </div>;
}
function TranscriptRecord({ record, agent, now }: { record: RecordItem; agent: AgentRun; now: number }) {
  if (record.kind === "tool") return <li className="record record-tool"><span className="record-marker"><Glyph name="tool" size={13} /></span><ToolEvidence tool={record.tool} now={now} /></li>;
  const message = record.message;
  return <li className={`record record-message message-${message.role}`}><span className="record-marker">{message.role === "user" ? <Glyph name="chat" size={12} /> : <span className={`mini-dot tone-${agent.definition.color}`} />}</span><div className="message-content"><div className="message-byline"><strong>{message.role === "user" ? `You → ${agent.definition.name}` : agent.definition.name}</strong><time>{time(message.at)}</time></div><p>{message.text}</p>{message.delivery && <span className={`delivery delivery-${message.delivery}`}><Glyph name={message.delivery === "queued" ? "clock" : "check"} size={12} />{message.delivery === "queued" ? "Queued · next response" : `Picked up at ${time(message.pickedUpAt ?? message.at)}`}</span>}</div></li>;
}
interface MessagingProps { onSend: (id: AgentId, value: string) => void; playing: boolean; drafts: Record<AgentId, string>; onDraft: (id: AgentId, value: string) => void }
function MessageComposer({ agent, onSend, playing, drafts, onDraft }: MessagingProps & { agent: AgentRun }) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const id = useId();
  const queued = queuedCount(agent);
  const draft = drafts[agent.definition.id] ?? "";
  const send = (event?: FormEvent) => { event?.preventDefault(); if (!draft.trim()) return; onSend(agent.definition.id, draft); onDraft(agent.definition.id, ""); textarea.current?.focus(); };
  return <form className="agent-composer" onSubmit={send}><label htmlFor={id}><Glyph name="chat" size={14} />Message {agent.definition.name}<span>Only this agent</span></label><div className="compose-input"><textarea id={id} ref={textarea} value={draft} onChange={event => onDraft(agent.definition.id, event.target.value)} placeholder={`A note for ${agent.definition.name}…`} rows={2} maxLength={4000} aria-describedby={`${id}-hint`} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }} /><button type="submit" className="send-note" disabled={!draft.trim()} aria-label={`Send message to ${agent.definition.name}`}><Glyph name="arrow" size={18} /></button></div><p id={`${id}-hint`} className="compose-hint">{queued > 0 ? <><span className="queued-pill">{queued} queued</span>{playing ? "Picked up at the next response." : "Demo paused. Play or advance to see delivery."}</> : <>Joins the next response. Running tools continue.</>}</p></form>;
}
interface InspectorProps extends MessagingProps { agent: AgentRun; state: ActivityState; compact?: boolean }
function AgentInspector({ agent, state, compact = false, ...messaging }: InspectorProps) {
  const [filter, setFilter] = useState<"all" | "messages" | "tools">("all");
  const records = agent.records.filter(record => filter === "all" || (filter === "tools" ? record.kind === "tool" : record.kind === "message"));
  const parent = state.agents[agent.definition.parentId]?.definition.name ?? "Assistant";
  return <section className={`agent-inspector${compact ? " inspector-compact" : ""}`} aria-label={`${agent.definition.name} activity and messages`}>
    {!compact && <header className="inspector-header"><div className="inspector-title"><Avatar agent={agent} /><div><h2>{agent.definition.name}</h2><Metadata agent={agent} /></div><span className="elapsed">{duration(agent, state.now)}</span></div><div className="inspector-status"><Badge status={agent.status} /><span><Glyph name="branch" size={12} />Reports to {parent}</span></div></header>}
    <div className="activity-column"><div className={`current-action action-${agent.status}`}><span>{agent.status === "running" ? <RunningBars /> : <Glyph name={agent.status === "completed" ? "check" : "alert"} size={16} />}</span><div><span className="eyebrow">{agent.status === "completed" ? "Result" : "Current activity"}</span><p>{agent.activity}</p></div></div><details className="assignment"><summary>Assigned task<Glyph name="chevron" size={12} /></summary><p>{agent.definition.assignment}</p></details><div className="activity-toolbar"><span>Activity</span><div className="segment-control" aria-label={`Filter ${agent.definition.name} activity`}>{(["all", "messages", "tools"] as const).map(item => <button key={item} type="button" aria-pressed={filter === item} onClick={() => setFilter(item)}>{item === "all" ? "All" : item === "messages" ? "Messages" : "Tools"}</button>)}</div></div><ol className="transcript">{records.map(record => <TranscriptRecord key={record.kind === "tool" ? record.tool.id : record.message.id} record={record} agent={agent} now={state.now} />)}{!records.length && <li className="empty-records">No {filter} events yet.</li>}</ol></div><MessageComposer agent={agent} {...messaging} />
  </section>;
}

function ParentConversation({ state, children }: { state: ActivityState; children?: ReactNode }) {
  const done = Object.values(state.agents).filter(agent => agent.status === "completed").length;
  return <section className="parent-conversation" aria-label="Main conversation"><div className="parent-message user-bubble"><span>You</span><p>Show images directly in chat, with a download action. Make sure it works on mobile.</p></div><div className="parent-message assistant-message"><div className="parent-byline"><span className="ngn-avatar">n</span><strong>ngn</strong><span>gpt-6.1-sol</span></div><p>I’ve split the work across research, implementation, and verification. You can follow each branch and message an agent directly.</p></div>{children}<div className="parent-result"><Glyph name="inbox" size={15} /><span>{done} of 4 agents returned results</span><span>{done === 4 ? "Ready for review" : "Work continues"}</span></div></section>;
}
interface ViewProps extends MessagingProps { state: ActivityState; selected: AgentId; onSelect: (id: AgentId) => void }
interface TreeProps extends ViewProps { variant: "inline" | "excerpt" | "outline"; expanded: Record<AgentId, boolean>; onToggle: (id: AgentId) => void; onInspect: (id: AgentId) => void; buttonRefs: RefObject<Record<AgentId, HTMLButtonElement | null>>; prefix: string }
function ConversationTree({ state, selected, variant, expanded, onToggle, onInspect, buttonRefs, prefix, ...messaging }: TreeProps) {
  function branch(agent: AgentRun): ReactNode {
    const { definition } = agent;
    const children = Object.values(state.agents).filter(child => child.definition.parentId === definition.id);
    const isExpanded = variant === "inline" && Boolean(expanded[definition.id]);
    const tool = latestTool(agent);
    const queue = queuedCount(agent);
    return <li key={definition.id} className="tree-branch"><article className={`tree-task${selected === definition.id ? " is-selected" : ""}${isExpanded ? " is-expanded" : ""}`}>
      <button ref={node => { buttonRefs.current[definition.id] = node; }} type="button" className="tree-task-summary" aria-expanded={variant === "inline" ? isExpanded : undefined} aria-controls={variant === "inline" ? `${prefix}-${definition.id}` : undefined} aria-label={`${variant === "inline" ? (isExpanded ? "Collapse" : "Expand") : "Inspect"} ${definition.name} · ${statusNames[agent.status]}`} onClick={() => variant === "inline" ? onToggle(definition.id) : onInspect(definition.id)}>
        <span className={`expand-chevron${isExpanded ? " open" : ""}`}><Glyph name="chevron" size={13} /></span><Avatar agent={agent} small /><span className="tree-task-copy"><span className="name-line"><strong>{definition.name}</strong><span className="role-name">{definition.role}</span></span><span className="tree-current">{agent.status === "running" && <RunningBars />}{agent.activity}</span></span><span className="tree-task-end"><Badge status={agent.status} /><span className="elapsed">{duration(agent, state.now)}{queue > 0 && <span className="queued-pill">{queue} queued</span>}</span></span>
      </button>
      {variant === "excerpt" && <div className="branch-excerpt"><p>{latestMessage(agent)}</p><div className="branch-excerpt-footer"><span>{tool ? <><Glyph name="tool" size={13} /><code>{tool.name}</code><span className={`inline-state state-${tool.status}`}>{tool.status === "waiting" ? "Awaiting approval" : tool.status}</span></> : <><Glyph name="check" size={13} />Result returned</>}</span><button type="button" className="text-button" onClick={() => onInspect(definition.id)}>Inspect &amp; message<Glyph name="arrow" size={13} /></button></div></div>}
      {variant === "inline" && <div id={`${prefix}-${definition.id}`} hidden={!isExpanded}>{isExpanded && <AgentInspector key={definition.id} agent={agent} state={state} {...messaging} compact />}</div>}
    </article>{children.length > 0 && <ul className="task-tree nested" aria-label={`Delegated by ${definition.name}`}>{children.map(branch)}</ul>}</li>;
  }
  return <ul className={`task-tree tree-${variant}`} aria-label="Conversation delegation tree">{Object.values(state.agents).filter(agent => agent.definition.parentId === "assistant").map(branch)}</ul>;
}

function TeamNavigation({ state, selected, onSelect, compact = false, collapsed = false }: { state: ActivityState; selected: AgentId; onSelect: (id: AgentId) => void; compact?: boolean; collapsed?: boolean }) {
  function branch(agent: AgentRun): ReactNode {
    const nested = agent.definition.parentId !== "assistant";
    const children = Object.values(state.agents).filter(child => child.definition.parentId === agent.definition.id);
    return <li key={agent.definition.id} className={nested ? "nested-nav" : ""}><button type="button" className={`agent-nav-item${selected === agent.definition.id ? " selected" : ""}`} aria-pressed={selected === agent.definition.id} aria-label={`${agent.definition.name} · ${statusNames[agent.status]}${queuedCount(agent) ? ` · ${queuedCount(agent)} queued messages` : ""}`} title={collapsed ? `${agent.definition.name} · ${agent.activity}` : undefined} onClick={() => onSelect(agent.definition.id)}><span className="nav-agent-line"><Avatar agent={agent} small /><span className="nav-agent-name"><strong>{agent.definition.name}</strong><span>{agent.definition.role}</span></span><span className={`nav-state state-${agent.status}`} /></span>{!collapsed && <><p>{agent.activity}</p><span className="nav-meta"><span className={`state-${agent.status}`}>{statusNames[agent.status]}</span><span>{queuedCount(agent) ? `${queuedCount(agent)} queued` : duration(agent, state.now)}</span></span></>}</button>{children.length > 0 && <ul aria-label={`Delegated by ${agent.definition.name}`}>{children.map(branch)}</ul>}</li>;
  }
  return <nav className={`team-navigation${compact ? " nav-compact" : ""}${collapsed ? " nav-collapsed" : ""}`} aria-label="Working team"><ul>{Object.values(state.agents).filter(agent => agent.definition.parentId === "assistant").map(branch)}</ul></nav>;
}
function TeamSummary({ state }: { state: ActivityState }) {
  const agents = Object.values(state.agents);
  return <div className="team-summary"><span><i className="state-running" />{agents.filter(agent => agent.status === "running").length} running</span><span><i className="state-waiting" />{agents.filter(agent => agent.status === "waiting" || agent.status === "failed").length} attention</span><span><i className="state-completed" />{agents.filter(agent => agent.status === "completed").length} done</span></div>;
}
function useNarrowScreen() {
  const [narrow, setNarrow] = useState(() => window.matchMedia("(max-width: 900px)").matches);
  useEffect(() => { const query = window.matchMedia("(max-width: 900px)"); const update = () => setNarrow(query.matches); query.addEventListener("change", update); update(); return () => query.removeEventListener("change", update); }, []);
  return narrow;
}
function TeamDrawer({ open, onClose, children }: { open: boolean; onClose: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const node = dialog.current; if (!node) return; if (open && !node.open) node.showModal(); if (!open && node.open) node.close(); }, [open]);
  return <dialog ref={dialog} className="team-drawer" aria-label="Working team" onCancel={onClose} onClose={onClose} onClick={event => { if (event.target === event.currentTarget) { const rect = event.currentTarget.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose(); } }}><header className="drawer-heading"><h2>Working team</h2><button type="button" className="icon-button" aria-label="Close working team" onClick={onClose}><Glyph name="close" /></button></header>{children}</dialog>;
}
function ReadOnlyComposer() { return <div className="parent-compose-placeholder" aria-label="Main conversation composer, read-only in demo"><p>What should we work on?</p><div><span><Glyph name="attach" size={15} />Attach</span><span><Glyph name="wave" size={15} />Voice</span><small>Main chat · demo read-only</small><span className="placeholder-send"><Glyph name="arrow" size={16} /></span></div></div>; }

function HybridView({ design, ...props }: ViewProps & { design: Design }) {
  const { state, selected, onSelect, ...messaging } = props;
  const [expanded, setExpanded] = useState<Record<AgentId, boolean>>({ [selected]: true });
  const [railOpen, setRailOpen] = useState(design !== "board");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [showFocus, setShowFocus] = useState(true);
  const narrow = useNarrowScreen();
  const prefix = useId();
  const buttonRefs = useRef<Record<AgentId, HTMLButtonElement | null>>({});
  const focusThread = useRef<HTMLDivElement>(null);
  const selectedAgent = state.agents[selected];
  const activeAgents = Object.values(state.agents).filter(agent => agent.status === "running").length;
  const attentionAgents = Object.values(state.agents).filter(agent => agent.status === "waiting" || agent.status === "failed").length;
  const variant = design === "tree" ? "inline" : design === "workspace" ? "excerpt" : "outline";

  useEffect(() => { if (!narrow) setDrawerOpen(false); }, [narrow]);
  const select = (id: AgentId, fromRail = false) => {
    onSelect(id);
    if (design === "tree") setExpanded(current => ({ ...current, [id]: true }));
    if (design === "workspace") setRailOpen(true);
    if (design === "board") setShowFocus(true);
    if (narrow) setDrawerOpen(design === "workspace");
    if (design === "board") window.requestAnimationFrame(() => { focusThread.current?.focus({ preventScroll: true }); focusThread.current?.scrollIntoView({ block: "start" }); });
    else if (fromRail && design === "tree") window.requestAnimationFrame(() => { buttonRefs.current[id]?.focus({ preventScroll: true }); buttonRefs.current[id]?.scrollIntoView({ block: "nearest" }); });
  };
  const toggle = (id: AgentId) => { onSelect(id); setExpanded(current => ({ ...current, [id]: !current[id] })); };
  const rail = (mobile = false) => <>
    {!mobile && <header className="rail-heading"><div><Glyph name="branch" size={17} /><h2>Working team</h2><span>4</span></div><button type="button" className="icon-button" aria-label="Collapse working team" onClick={() => setRailOpen(false)}><Glyph name="collapse" size={16} /></button></header>}
    <TeamSummary state={state} />
    <div className="team-root"><span className="ngn-avatar">n</span><div><strong>Assistant</strong><span>Main conversation</span></div><span className="team-root-count">3 branches</span></div>
    <TeamNavigation state={state} selected={selected} onSelect={id => { select(id, true); if (mobile && design !== "workspace") setDrawerOpen(false); }} compact={design === "workspace"} />
    {design === "workspace" ? <div className="rail-inspector"><div className="rail-section-label"><Glyph name="focus" size={13} />Selected branch</div><AgentInspector key={selected} agent={selectedAgent} state={state} {...messaging} /></div> : <div className="rail-bottom-note"><Glyph name="chat" size={15} /><div><strong>{design === "tree" ? "All the detail stays in chat" : "A little room for the team"}</strong><p>{design === "tree" ? "Choose an agent to jump to its activity and messages." : "Choose a branch to focus its conversation. Collapse this panel to keep just the agent shortcuts."}</p></div></div>}
  </>;

  return <div className={`app-preview hybrid-${design}${railOpen ? " rail-open" : " rail-collapsed"}`}><header className="app-header"><div className="session-heading"><span className="session-breadcrumb">workspace<Glyph name="chevron" size={12} />Conversation</span><h2>Image experience</h2></div><span className="app-header-state">{activeAgents > 0 ? <RunningBars /> : <Glyph name={attentionAgents > 0 ? "alert" : "check"} size={14} />}{activeAgents > 0 ? `${activeAgents} agents working` : attentionAgents > 0 ? "Needs attention" : "Team finished"}</span><button type="button" className="team-toggle" aria-label={narrow ? "Open working team" : railOpen ? "Collapse working team" : "Expand working team"} aria-expanded={narrow ? drawerOpen : railOpen} onClick={() => narrow ? setDrawerOpen(true) : setRailOpen(value => !value)}><Glyph name="panel" size={17} /><span>Working team</span><span className="count-badge">4</span></button></header>
    <div className="hybrid-layout"><div className="chat-area"><div className="conversation-scroll"><ParentConversation state={state}><div className="tree-group-heading"><span><Glyph name="branch" size={16} />Delegated work<span className="group-count">4</span></span><span>{design === "tree" ? "Expand to inspect or message" : design === "workspace" ? "Details open on the right" : "Choose a branch to focus"}</span></div><ConversationTree {...props} variant={variant} expanded={expanded} onToggle={toggle} onInspect={select} buttonRefs={buttonRefs} prefix={prefix} />
      {design === "board" && <div className={`focus-thread${showFocus ? " is-open" : ""}`} ref={focusThread} tabIndex={-1}>{showFocus ? <><div className="focus-thread-heading"><div><Glyph name="branch" size={14} /><span>Assistant</span>{selectedAgent.definition.parentId !== "assistant" && <><Glyph name="chevron" size={12} /><button type="button" className="text-button" onClick={() => select(selectedAgent.definition.parentId)}>{state.agents[selectedAgent.definition.parentId].definition.name}</button></>}<Glyph name="chevron" size={12} /><strong>{selectedAgent.definition.name}</strong></div><button type="button" className="text-button" onClick={() => setShowFocus(false)}><Glyph name="close" size={13} />Close thread</button></div><AgentInspector key={selected} agent={selectedAgent} state={state} {...messaging} /></> : <button type="button" className="reopen-thread" onClick={() => setShowFocus(true)}><Avatar agent={selectedAgent} small /><span>Open {selectedAgent.definition.name}’s thread</span><Glyph name="arrow" size={16} /></button>}</div>}
    </ParentConversation></div><ReadOnlyComposer /></div>
    {!narrow && (railOpen ? <aside className="team-panel" aria-label="Working team panel">{rail()}</aside> : <aside className="mini-rail" aria-label="Working team shortcuts"><button type="button" className="icon-button" aria-label="Expand working team" title="Expand working team" onClick={() => setRailOpen(true)}><Glyph name="panel" size={18} /></button><TeamNavigation state={state} selected={selected} onSelect={id => { select(id, true); if (design === "workspace") setRailOpen(true); }} collapsed /><span className="mini-rail-label">Team</span></aside>)}
    </div>{narrow && <TeamDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)}>{rail(true)}</TeamDrawer>}
  </div>;
}

export function App() {
  const hash = window.location.hash.slice(1);
  const [design, setDesign] = useState<Design>(designs.some(item => item.id === hash) ? hash as Design : "tree");
  const [state, setState] = useState(startDemo);
  const [selected, setSelected] = useState<AgentId>("research");
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [drafts, setDrafts] = useState<Record<AgentId, string>>({});
  const active = designs.find(item => item.id === design)!;
  const ended = state.now >= DEMO_END && Object.keys(state.followUps).length === 0;
  useEffect(() => { if (!playing || ended) return; const timer = window.setInterval(() => setState(current => advanceTo(current, current.now + 1)), 1000 / speed); return () => window.clearInterval(timer); }, [playing, speed, ended]);
  useEffect(() => { if (ended) setPlaying(false); }, [ended]);
  const onSend = (id: AgentId, value: string) => setState(current => queueMessage(current, id, value));
  const onDraft = (id: AgentId, value: string) => setDrafts(current => ({ ...current, [id]: value }));
  const viewProps = { state, selected, onSelect: setSelected, onSend, playing, drafts, onDraft };
  return <div className={`studio design-${design}${playing ? " is-playing" : " is-paused"}`}><a className="skip-link" href="#design-preview">Skip to interactive design</a><header className="studio-header"><div className="studio-brand"><span className="brand-mark">n</span><div><span className="eyebrow">ngn / subagent interactions</span><h1>One conversation. A working team.</h1></div></div><div className="demo-label"><span className="revision-pill">Revision 02</span><span>Interactive study · simulated agents</span></div></header>
    <div className="design-switcher" aria-label="Choose a design direction">{designs.map(item => <button type="button" key={item.id} className={`design-choice${design === item.id ? " active" : ""}`} aria-pressed={design === item.id} onClick={() => { setDesign(item.id); window.history.replaceState(null, "", `#${item.id}`); }}><span className="option-letter">{item.letter}</span><span><strong>{item.name}</strong><small>{item.description}</small></span><Glyph name={item.id === "tree" ? "branch" : item.id === "workspace" ? "panel" : "focus"} size={19} /></button>)}</div>
    <section className="simulation-controls" aria-label="Simulated event timeline"><div className="transport-buttons"><button type="button" className="play-button" onClick={() => { if (ended) setState(startDemo()); setPlaying(value => !value); }}><Glyph name={playing ? "pause" : "play"} size={14} />{playing ? "Pause" : ended ? "Replay" : "Play demo"}</button><button type="button" title="Advance to the next event" onClick={() => { setPlaying(false); setState(current => advanceTo(current, nextEventTime(current))); }}><Glyph name="next" size={14} /><span>Next event</span></button><button type="button" className="reset-button icon-button" aria-label="Reset demo and clear messages" onClick={() => { setState(startDemo()); setDrafts({}); setPlaying(false); }}><Glyph name="reset" size={16} /></button></div><div className="timeline-progress"><div><span>{playing ? "Simulated activity playing" : ended ? "Demo complete" : "Timeline paused"}</span><time>{time(state.now)} <span>/ {time(DEMO_END)}</span></time></div><progress value={Math.min(state.now, DEMO_END)} max={DEMO_END} aria-label="Demo timeline" /></div><label className="speed-control">Speed<select value={speed} onChange={event => setSpeed(Number(event.target.value))}><option value={1}>1×</option><option value={2}>2×</option><option value={4}>4×</option></select></label></section>
    <div className="design-caption"><p><strong>{active.letter}.</strong> {active.detail}</p><span>No backend connection</span></div><main id="design-preview" tabIndex={-1}><HybridView key={design} design={design} {...viewProps} /></main><footer className="studio-footer"><span>Messages, tool arguments and results are illustrative.</span><span>Try: message Explorer, then advance to 0:13 to see it picked up.</span></footer><p className="sr-only" role="status" aria-live="polite">{state.announcement}</p></div>;
}
