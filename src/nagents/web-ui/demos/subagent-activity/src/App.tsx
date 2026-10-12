import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode, RefObject } from "react";
import { advanceTo, nextEventTime, queuedCount, queueMainMessage, queueMessage, startDemo, visibleAgents } from "./engine";
import { DEMO_END } from "./fixtures";
import { buildActivityTree, pageActivityTree } from "./activityTree";
import type { ActivityTreeNode } from "./activityTree";
import { resolveAgentLifecycle } from "./agentLifecycle";
import { AgentLifecycleView, LifecycleStatus, useArrivalStyle } from "./AgentLifecycleView";
import type { ArrivalTimes } from "./AgentLifecycleView";
import { useAgentArrivals } from "./useAgentArrivals";
import { MAIN_AGENT_ID, mainAgentSnapshot, resolveMainRuntime } from "./mainRuntime";
import { TeamIcon } from "./TeamIcons";
import { applyLaunchSettings, copyRuntimeSettings, createRuntimeSettings, resolveAgentRuntime } from "./agentRuntime";
import type { RuntimeSettings, RuntimeSettingsMap } from "./agentRuntime";
import { AgentSettingsDialog } from "./AgentSettingsDialog";
import type { ActivityState, AgentId, AgentRun, MainMessage, RecordItem, Status, ToolCall } from "./types";

type Design = "more" | "drawer";
type ThreadFilter = "all" | "messages" | "tools";
type Phase = "closed" | "open" | "closing";
const designs: { id: Design; letter: string; name: string; description: string; detail: string }[] = [
  { id: "more", letter: "A", name: "Activity", description: "Compact agent events in the chat", detail: "Short activity rows in the conversation. The team tree orders branches by their latest activity, including nested agents. One message box follows the conversation you select." },
  { id: "drawer", letter: "B", name: "Timeline", description: "A quiet timeline of agent activity", detail: "Spawn times form a slim conversation timeline. The same activity tree and shared message box stay available. Two delegation levels are expanded by default." },
];
function Glyph({ name, size = 18 }: { name: string; size?: number }) {
  if (name === "branch") return <TeamIcon size={size} />;
  const paths: Record<string, string> = {
    chat: "M4 3h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H8l-5 3V5a2 2 0 0 1 1-2Z", arrow: "M4 10h12m-5-5 5 5-5 5", chevron: "m7 4 6 6-6 6", check: "m4 10 4 4 8-8",
    clock: "M10 5v5l3 2M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0", play: "m6 3 10 7-10 7Z", pause: "M7 3v14M13 3v14", reset: "M4 7a7 7 0 1 1-1 7M4 2v5H0", next: "m3 4 8 6-8 6ZM15 4v12",
    tool: "m7 5-5 5 5 5m6-10 5 5-5 5m-2-13-2 16", alert: "M10 5v6m0 3v.1M10 1 1 18h18Z", panel: "M2 3h16v14H2ZM12 3v14", close: "m5 5 10 10M15 5 5 15", back: "M16 10H4m5-5-5 5 5 5",
    settings: "M3 5h14M3 10h14M3 15h14M7 3v4M13 8v4M8 13v4", info: "M10 9v5m0-8v.1M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
    attach: "m7 10 5-5a2.5 2.5 0 0 1 3.5 3.5l-6 6a4 4 0 0 1-5.7-5.7l6-6M6.5 11.5l5-5", wave: "M2 8v4M6 5v10M10 2v16M14 5v10M18 8v4",
  };
  return <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] ?? paths.chat} /></svg>;
}
const statusNames: Record<Status, string> = { running: "Running", waiting: "Waiting", failed: "Failed", completed: "Done" };
function time(seconds: number) { return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`; }
function duration(agent: AgentRun, now: number) { return `${Math.max(0, (agent.finishedAt ?? now) - agent.startedAt)}s`; }
function parentName(agent: AgentRun, state: ActivityState) { return state.agents[agent.definition.parentId]?.definition.name ?? "Main assistant"; }
function RunningBars() { return <span className="running-bars" aria-hidden="true"><i /><i /><i /><i /></span>; }
function StateMark({ status, text = false }: { status: Status; text?: boolean }) { return <span className={`state-mark state-${status}`} aria-label={text ? undefined : statusNames[status]}>{status === "running" ? <RunningBars /> : <Glyph name={status === "completed" ? "check" : "alert"} size={13} />}{text && statusNames[status]}</span>; }
type OpenAgent = (id: AgentId, trigger: HTMLElement, preserveOpener?: boolean) => void;
interface MessagingProps { onSend: (id: AgentId, value: string) => void; playing: boolean; drafts: Record<AgentId, string>; onDraft: (id: AgentId, value: string) => void }
interface ViewProps extends MessagingProps, OverviewProps { state: ActivityState; selected: AgentId; onSelect: (id: AgentId) => void; mainDraft: string; onMainDraft: (value: string) => void; onMainSend: (value: string) => void; acceptedSettings: RuntimeSettingsMap; savedSettings: RuntimeSettingsMap; onConfigure: (id: AgentId) => void }
function InspectButton({ agent, onOpen, short = false }: { agent: AgentRun; onOpen: OpenAgent; short?: boolean }) { return <button type="button" className="inspect-action" aria-label={`Inspect & message ${agent.definition.name}`} onClick={event => onOpen(agent.definition.id, event.currentTarget)}><span>{short ? "Inspect" : "Inspect & message"}</span><Glyph name="arrow" size={14} /></button>; }

function tokenLabel(tokens: number) { return tokens >= 1000 ? `${(tokens / 1000).toFixed(tokens >= 100000 ? 0 : 1).replace(/\.0$/, "")}k` : String(tokens); }
function settingsPending(id: AgentId, saved: RuntimeSettingsMap, accepted: RuntimeSettingsMap) { return (["provider", "model", "compact_trigger", "compact_tokens", "compact_messages"] as const).some(key => saved[id]?.[key] !== accepted[id]?.[key]); }
function SettingsButton({ agent, onConfigure, pending = false }: { agent: AgentRun; onConfigure: (id: AgentId) => void; pending?: boolean }) { return <button type="button" className={`agent-settings-button${pending ? " has-pending" : ""}`} aria-label={`Settings for ${agent.definition.name}${pending ? ", changes saved for next run" : ""}`} title={`Settings for ${agent.definition.name}${pending ? " · next-run changes" : ""}`} onClick={() => onConfigure(agent.definition.id)}><Glyph name="settings" size={14} />{pending && <i aria-hidden="true" />}</button>; }
function compactPolicy(runtime: ReturnType<typeof resolveAgentRuntime>) {
  if (runtime.contextOverflow) return "Window exceeded";
  if (runtime.compactionDue) return "Compact next";
  if (runtime.compactThresholdTokens !== undefined) return `Compact at ${tokenLabel(runtime.compactThresholdTokens)}`;
  if (runtime.compactThresholdMessages !== undefined) return `Compact at ${runtime.compactThresholdMessages} msgs`;
  return "Compaction off";
}
interface OverviewProps { arrivalTimes: ArrivalTimes; treeLimit: number; onTreeLimit: (limit: number) => void; expandedAgents: Record<AgentId, boolean>; onToggleBranch: (id: AgentId, expanded: boolean) => void }
interface RailProps extends OverviewProps { design: Design; state: ActivityState; selected: AgentId; onOpen: OpenAgent; onMain: () => void; headingRef: RefObject<HTMLHeadingElement | null>; onClose?: () => void; railVisible: boolean; acceptedSettings: RuntimeSettingsMap; savedSettings: RuntimeSettingsMap; onConfigure: (id: AgentId) => void }
function CompactAgentRow({ agent, design, state, selected, onOpen, acceptedSettings, savedSettings, onConfigure, arrivalAt = 0, activityAt, onMain }: Pick<RailProps, "design" | "state" | "selected" | "onOpen" | "acceptedSettings" | "savedSettings" | "onConfigure"> & { agent: AgentRun; arrivalAt?: number; activityAt: number; onMain?: () => void }) {
  const isMain = agent.definition.id === MAIN_AGENT_ID;
  const lifecycle = resolveAgentLifecycle(agent, state);
  const arrivalMotion = useArrivalStyle(arrivalAt);
  const runtime = isMain ? resolveMainRuntime(state, acceptedSettings) : resolveAgentRuntime(agent, state, acceptedSettings);
  const pending = settingsPending(agent.definition.id, savedSettings, acceptedSettings);
  const fraction = Math.min(100, runtime.estimatedTokens / runtime.modelContextLimit * 100);
  const threshold = runtime.compactThresholdTokens === undefined ? undefined : Math.min(100, runtime.compactThresholdTokens / runtime.modelContextLimit * 100);
  const contextAlert = runtime.contextOverflow ? "Context window exceeded" : runtime.compactionDue ? "Compaction due before the next response" : "";
  const metric = <span className="row-context-value">~{tokenLabel(runtime.estimatedTokens)}<span> / {tokenLabel(runtime.modelContextLimit)}</span></span>;
  const model = <span className="row-model-name">{runtime.providerLabel}<span> · </span>{runtime.model}</span>;
  return <article data-agent-id={agent.definition.id} data-main-overview={isMain ? "" : undefined} className={`compact-agent-row compact-f1a${selected === agent.definition.id ? " is-selected" : ""}${runtime.compactionDue ? " context-due" : ""}${runtime.contextOverflow ? " context-overflow" : ""}${arrivalAt ? " agent-arriving" : ""}`} style={arrivalMotion}>
    <button type="button" className="team-person" aria-label={`${isMain ? "Go to main conversation" : `Inspect & message ${agent.definition.name}`} · ${lifecycle.originLabel} at ${time(lifecycle.startedAt)} · ${lifecycle.label}: ${lifecycle.detail} · ${runtime.providerLabel} ${runtime.model} · approximately ${runtime.estimatedTokens} of ${runtime.modelContextLimit} context tokens${contextAlert ? ` · ${contextAlert}` : ""}${queuedCount(agent) ? ` · ${queuedCount(agent)} queued messages` : ""}`} onClick={event => { if (isMain) onMain?.(); else onOpen(agent.definition.id, event.currentTarget); }}>
      <span className="row-top"><strong>{agent.definition.name}</strong><LifecycleStatus lifecycle={lifecycle} />{lifecycle.queuedMessages > 0 && <span className="row-queued" aria-label={`${lifecycle.queuedMessages} queued messages`}>{lifecycle.queuedMessages}</span>}</span>
      <span className="row-model-context">{model}<time className="row-last-activity" title={`Latest branch activity, including subagents: ${time(activityAt)}`}>{time(activityAt)}</time></span>
      <span className="row-context-policy">{metric}<span>{compactPolicy(runtime)}</span></span>
      <span className="row-meter" aria-hidden="true"><i style={{ width: `${fraction}%` }} />{threshold !== undefined && <b style={{ left: `${threshold}%` }} />}</span>
    </button><SettingsButton agent={agent} onConfigure={onConfigure} pending={pending} />
  </article>;
}
function MainConversationHeader({ state, acceptedSettings, savedSettings, onConfigure }: Pick<RailProps, "state" | "acceptedSettings" | "savedSettings" | "onConfigure">) {
  const main = mainAgentSnapshot(state, acceptedSettings);
  const runtime = resolveMainRuntime(state, acceptedSettings);
  return <header className="main-conversation-heading"><div><div className="thread-title"><h2>Main assistant</h2><LifecycleStatus lifecycle={resolveAgentLifecycle(main, state)} /></div><p className="main-live-detail">{resolveAgentLifecycle(main, state).detail}</p><span className="thread-model">{runtime.providerLabel}<span>·</span>{runtime.model}<span className="thread-context-reading">~{tokenLabel(runtime.estimatedTokens)} / {tokenLabel(runtime.modelContextLimit)} tokens</span></span></div><SettingsButton agent={main} onConfigure={onConfigure} pending={settingsPending(MAIN_AGENT_ID, savedSettings, acceptedSettings)} /></header>;
}
function TeamRail({ design, state, selected, onOpen, onMain, headingRef, onClose, railVisible, acceptedSettings, savedSettings, onConfigure, arrivalTimes, treeLimit, onTreeLimit, expandedAgents, onToggleBranch }: RailProps) {
  const scroll = useRef<HTMLDivElement>(null);
  const sentinel = useRef<HTMLDivElement>(null);
  const tree = buildActivityTree(state, mainAgentSnapshot(state, acceptedSettings));
  let pageSelection = selected;
  let ancestor = tree.nodesById.get(selected);
  const walked = new Set<AgentId>();
  while (ancestor && !walked.has(ancestor.agent.definition.id)) {
    walked.add(ancestor.agent.definition.id);
    ancestor = tree.nodesById.get(ancestor.agent.definition.parentId);
    if (ancestor && expandedAgents[ancestor.agent.definition.id] === false) pageSelection = ancestor.agent.definition.id;
  }
  const page = pageActivityTree(tree, treeLimit, pageSelection, { expandedIds: new Set(Object.keys(expandedAgents).filter(id => expandedAgents[id])), defaultDepth: 2 });
  const loadMore = () => onTreeLimit(treeLimit + 4);
  useEffect(() => {
    if (!railVisible || !page.remainingCount || !scroll.current || !sentinel.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) onTreeLimit(treeLimit + 4); }, { root: scroll.current, rootMargin: "0px 0px 24px 0px" });
    observer.observe(sentinel.current);
    return () => observer.disconnect();
  }, [railVisible, page.remainingCount, treeLimit, onTreeLimit]);
  const common = { design, state, selected, onOpen, onMain, acceptedSettings, savedSettings, onConfigure };
  function branch(node: ActivityTreeNode, depth: number): ReactNode {
    const full = tree.nodesById.get(node.agent.definition.id)!;
    const deepToggle = depth >= 2 && full.children.length > 0;
    const expanded = Boolean(expandedAgents[node.agent.definition.id]) || node.children.length > 0;
    return <li className="activity-tree-node" data-tree-agent={node.agent.definition.id} data-tree-depth={depth} data-branch-activity={node.latestActivityAt} key={node.agent.definition.id}>
      <CompactAgentRow agent={node.agent} {...common} arrivalAt={arrivalTimes[node.agent.definition.id]} activityAt={node.latestActivityAt} />
      {deepToggle && <button type="button" className="tree-branch-toggle" aria-expanded={expanded} aria-label={`${expanded ? "Collapse" : "Expand"} subagents of ${node.agent.definition.name}`} onClick={() => onToggleBranch(node.agent.definition.id, !expanded)}><Glyph name={expanded ? "back" : "chevron"} size={11} />{full.children.length} {full.children.length === 1 ? "subagent" : "subagents"}</button>}
      {node.children.length > 0 && <ul aria-label={`Subagents of ${node.agent.definition.name}`}>{node.children.map(child => branch(child, depth + 1))}</ul>}
    </li>;
  }
  return <section className="team-rail activity-team-rail" aria-label="Agent tree"><header><h2 ref={headingRef} tabIndex={-1}><TeamIcon size={17} />Team <span>{tree.totalCount}</span></h2>{onClose && <button type="button" className="icon-button" aria-label="Close working team" onClick={onClose}><Glyph name="close" /></button>}</header>
    <div className="activity-tree-scroll" ref={scroll}><ul className="activity-tree" aria-label="Agents ordered by latest branch activity">{branch(page.root, 0)}</ul>{page.remainingCount > 0 && <div className="tree-load-sentinel" ref={sentinel}><button type="button" className="tree-load-more" onClick={loadMore}>Load more <span>{page.remainingCount}</span></button></div>}</div>
  </section>;
}

function MainMessageView({ message, state, onOpen, acceptedSettings, children }: { message: MainMessage; state: ActivityState; onOpen: OpenAgent; acceptedSettings: RuntimeSettingsMap; children?: ReactNode }) {
  const mainRuntime = resolveMainRuntime(state, acceptedSettings);
  if (message.returnedAgentId) { const agent = state.agents[message.returnedAgentId]; return <article className="returned-message" data-main-message={message.id}><details className="returned-result"><summary aria-label={`${agent.definition.name} result at ${time(message.at)}`}><Glyph name="check" size={12} /><strong>{agent.definition.name} returned</strong><time>{time(message.at)}</time><span className="returned-preview">{message.text}</span><Glyph name="chevron" size={12} /></summary><p className="returned-body">{message.text}</p></details><InspectButton agent={agent} onOpen={onOpen} short /></article>; }
  return <article className={`main-message main-message-${message.role}`} data-main-message={message.id}>{message.role === "assistant" ? <div className="assistant-byline"><span className="ngn-avatar">n</span><strong>ngn</strong><span>{mainRuntime.providerLabel} · {mainRuntime.model}</span><time>{time(message.at)}</time></div> : <div className="user-byline"><strong>You</strong><time>{time(message.at)}</time></div>}<p>{message.text}</p>{children}</article>;
}
function ConversationComposer({ agent, value, onChange, onSend, playing, queued }: { agent: AgentRun; value: string; onChange: (value: string) => void; onSend: (id: AgentId, value: string) => void; playing: boolean; queued: number }) {
  const id = useId();
  const textarea = useRef<HTMLTextAreaElement>(null);
  const send = (event?: FormEvent) => { event?.preventDefault(); if (!value.trim()) return; onSend(agent.definition.id, value); onChange(""); textarea.current?.focus(); };
  return <form className="main-composer conversation-composer" data-recipient={agent.definition.id} onSubmit={send}><label htmlFor={id}><span className="composer-recipient-avatar" aria-hidden="true">{agent.definition.initials}</span>Message {agent.definition.name}<span>{queued > 0 ? `${queued} queued` : ""}</span></label><div><textarea id={id} ref={textarea} rows={1} value={value} onChange={event => onChange(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }} placeholder={`Message ${agent.definition.name}…`} maxLength={4000} aria-describedby={`${id}-hint`} /><button type="submit" className="send-main" aria-label={`Send message to ${agent.definition.name}`} disabled={!value.trim()}><Glyph name="arrow" size={18} /></button></div><p id={`${id}-hint`}>{queued > 0 ? `Queued for the next response.${playing ? "" : " Play or advance to see delivery."}` : "Enter to send · Shift+Enter for a new line"}</p></form>;
}

/* Child conversations expose published messages and tool evidence, not private reasoning. */
function ToolRow({ tool, now }: { tool: ToolCall; now: number }) {
  const id = useId();
  let output = tool.output;
  try { output = JSON.stringify(JSON.parse(tool.output), null, 2); } catch { /* Keep literal shell/text output. */ }
  return <details className={`tool-row tool-${tool.status}`}><summary><StateMark status={tool.status} /><span><span className="tool-line"><code>{tool.name}</code><span>{tool.status === "waiting" ? "Awaiting approval" : tool.status === "running" ? `${now - tool.startedAt}s` : `${(tool.finishedAt ?? now) - tool.startedAt}s · ${tool.status === "failed" ? "failed" : "done"}`}</span></span><span className="tool-title">{tool.title}</span></span><Glyph name="chevron" size={12} /></summary>{tool.status === "running" && <span className="tool-progress" aria-hidden="true" />}<div className="tool-detail"><p>{tool.summary}</p><h4 id={`${id}-args`}>Arguments</h4><pre aria-labelledby={`${id}-args`}>{JSON.stringify(tool.arguments, null, 2)}</pre><h4 id={`${id}-output`}>{tool.status === "running" ? "Output so far" : "Result"}</h4><pre aria-labelledby={`${id}-output`}>{output || "Not started. No output yet."}</pre></div></details>;
}
function RecordView({ record, agent, now }: { record: RecordItem; agent: AgentRun; now: number }) {
  if (record.kind === "tool") return <li className="thread-tool"><ToolRow tool={record.tool} now={now} /></li>;
  const message = record.message;
  return <li className={`thread-message message-${message.role}`}><div className="message-byline"><strong>{message.role === "user" ? "You → " + agent.definition.name : message.returnedAgentId ? "Delegation result" : agent.definition.name}</strong><time>{time(message.at)}</time></div><p>{message.text}</p>{message.delivery && <span className={`delivery delivery-${message.delivery}`}><Glyph name={message.delivery === "queued" ? "clock" : "check"} size={12} />{message.delivery === "queued" ? "Queued · next response" : `Picked up at ${time(message.pickedUpAt ?? message.at)}`}</span>}</li>;
}
interface AgentThreadProps { scrollRequest: number; state: ActivityState; agent: AgentRun; filter: ThreadFilter; onFilter: (filter: ThreadFilter) => void; onClose: () => void; onOrigin: (id: string) => void; onOpen: OpenAgent; headingRef: RefObject<HTMLHeadingElement | null>; scrollPositions: RefObject<Record<string, number>>; design: Design; acceptedSettings: RuntimeSettingsMap; savedSettings: RuntimeSettingsMap; onConfigure: (id: AgentId) => void }
function AgentThread({ state, agent, filter, onFilter, onClose, onOrigin, onOpen, headingRef, scrollPositions, design, acceptedSettings, savedSettings, onConfigure, scrollRequest }: AgentThreadProps) {
  const body = useRef<HTMLDivElement>(null);
  const key = `${agent.definition.id}:${filter}`;
  const handledScroll = useRef(scrollRequest);
  const runtime = resolveAgentRuntime(agent, state, acceptedSettings);
  useLayoutEffect(() => { const node = body.current; if (!node) return; node.scrollTop = scrollPositions.current[key] ?? 0; return () => { scrollPositions.current[key] = node.scrollTop; }; }, [key, scrollPositions]);
  const records = agent.records.filter(record => filter === "all" || (filter === "tools" ? record.kind === "tool" : record.kind === "message"));
  useEffect(() => { if (handledScroll.current === scrollRequest) return; handledScroll.current = scrollRequest; if (body.current) body.current.scrollTop = body.current.scrollHeight; }, [scrollRequest]);
  return <section className="agent-thread-panel" aria-label={`${agent.definition.name} activity and messages`}><header className="thread-header"><div className="thread-breadcrumb"><button type="button" onClick={() => onOrigin(agent.definition.originMessageId)}><Glyph name="back" size={12} />Main conversation</button>{agent.definition.parentId !== "assistant" && <><Glyph name="chevron" size={11} /><button type="button" onClick={event => onOpen(agent.definition.parentId, event.currentTarget, true)}>{parentName(agent, state)}</button></>}</div><button type="button" className="icon-button" aria-label="Close agent conversation" onClick={() => onClose()}><Glyph name="close" size={17} /></button><div className="thread-title"><h2 ref={headingRef} tabIndex={-1}>{agent.definition.name}</h2><LifecycleStatus lifecycle={resolveAgentLifecycle(agent, state)} /><time>{duration(agent, state.now)}</time></div><span className="thread-model">{runtime.providerLabel}<span>·</span>{runtime.model}<span className="thread-context-reading">~{tokenLabel(runtime.estimatedTokens)} / {tokenLabel(runtime.modelContextLimit)} tokens</span></span></header><div className="thread-controls"><SettingsButton agent={agent} onConfigure={onConfigure} pending={settingsPending(agent.definition.id, savedSettings, acceptedSettings)} /><details className="assignment"><summary>Delegation details<Glyph name="chevron" size={11} /></summary><div className="delegation-details"><p>Illustrative delegation arguments and accepted run settings. Omitted values inherit the agent profile; saved changes apply to the next run.</p><pre aria-label={`${agent.definition.name} delegation arguments`}>{JSON.stringify({ delegate: { name: agent.definition.name, task: agent.definition.assignment, provider: runtime.providerId, model: runtime.model }, runtime: { compact_trigger: runtime.settings.compact_trigger, compact_tokens: runtime.settings.compact_tokens, compact_messages: runtime.settings.compact_messages } }, null, 2)}</pre></div></details><div className="thread-tabs" aria-label={`Filter ${agent.definition.name} activity`}>{(["all", "messages", "tools"] as const).map(value => <button type="button" key={value} aria-pressed={filter === value} onClick={() => onFilter(value)}>{value === "all" ? "Conversation" : value === "messages" ? "Messages" : "Tools"}</button>)}</div></div><div className="thread-scroll" ref={body}><ol className="agent-thread">{records.map(record => <RecordView key={record.kind === "tool" ? record.tool.id : record.message.id} record={record} agent={agent} now={state.now} />)}{records.length === 0 && <li className="empty-thread">No {filter === "tools" ? "tool calls" : "messages"} yet.</li>}</ol></div></section>;
}
function useMediaQuery(query: string) { const [matches, setMatches] = useState(() => window.matchMedia(query).matches); useEffect(() => { const media = window.matchMedia(query); const update = () => setMatches(media.matches); media.addEventListener("change", update); update(); return () => media.removeEventListener("change", update); }, [query]); return matches; }
function Drawer({ open, closing = false, label, onClose, children, kind = "team" }: { open: boolean; closing?: boolean; label: string; onClose: () => void; children: ReactNode; kind?: "team" | "agent" }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const node = dialog.current; if (!node) return; if (open && !node.open) node.showModal(); if (!open && node.open) node.close(); }, [open]);
  return <dialog ref={dialog} className={`mobile-drawer drawer-${kind}${closing ? " is-closing" : ""}`} aria-label={label} onCancel={event => { event.preventDefault(); event.stopPropagation(); onClose(); }} onClick={event => { if (event.target !== event.currentTarget) return; const rect = event.currentTarget.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose(); }}>{children}</dialog>;
}
function HybridView({ design, ...props }: ViewProps & { design: Design }) {
  const { state, selected, onSelect, mainDraft, onMainDraft, onMainSend, acceptedSettings, savedSettings, onConfigure, arrivalTimes, treeLimit, onTreeLimit, expandedAgents, onToggleBranch, ...messaging } = props;
  const [active, setActive] = useState<AgentId>("");
  const [phase, setPhase] = useState<Phase>("closed");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [messageScroll, setMessageScroll] = useState(0);
  const [filters, setFilters] = useState<Record<AgentId, ThreadFilter>>({});
  const narrow = useMediaQuery("(max-width: 900px)");
  const reduceMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const opener = useRef<HTMLElement | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const teamHeading = useRef<HTMLHeadingElement>(null);
  const teamToggle = useRef<HTMLButtonElement>(null);
  const mainScroll = useRef<HTMLDivElement>(null);
  const mainPosition = useRef(0);
  const threadPositions = useRef<Record<string, number>>({});
  const replayTimer = useRef<number | undefined>(undefined);
  const pendingOrigin = useRef<string | undefined>(undefined);
  const restoreOpener = useRef(true);
  const restoreAfterClose = useRef(false);
  const nearBottom = useRef(true);
  const visible = phase !== "closed" && Boolean(state.agents[active]);
  const agents = visibleAgents(state);
  const motionTime = reduceMotion ? 0 : 210;
  useEffect(() => () => window.clearTimeout(replayTimer.current), []);
  useEffect(() => { if (!narrow) setDrawerOpen(false); }, [narrow]);
  const scrollToOrigin = (id: string) => {
    const node = mainScroll.current;
    const message = node?.querySelector<HTMLElement>(`[data-main-message="${CSS.escape(id)}"]`);
    if (node && message) node.scrollTo({ top: node.scrollTop + message.getBoundingClientRect().top - node.getBoundingClientRect().top, behavior: reduceMotion ? "instant" : "smooth" });
  };
  useEffect(() => {
    if (phase !== "closing") return;
    const timer = window.setTimeout(() => { restoreAfterClose.current = true; setPhase("closed"); }, motionTime);
    return () => window.clearTimeout(timer);
  }, [phase, motionTime]);
  useEffect(() => {
    if (phase !== "closed" || !restoreAfterClose.current) return;
    restoreAfterClose.current = false;
    const frame = window.requestAnimationFrame(() => {
      mainScroll.current?.scrollTo({ top: mainPosition.current, behavior: "instant" });
      if (pendingOrigin.current) { scrollToOrigin(pendingOrigin.current); pendingOrigin.current = undefined; }
      if (restoreOpener.current) {
        if (opener.current?.isConnected && opener.current.getClientRects().length) opener.current.focus({ preventScroll: true });
        else teamToggle.current?.focus({ preventScroll: true });
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [phase]);
  useEffect(() => { if (phase !== "open") return; const frame = window.requestAnimationFrame(() => heading.current?.focus({ preventScroll: true })); return () => window.cancelAnimationFrame(frame); }, [phase, active]);
  useEffect(() => { const node = mainScroll.current; if (node && nearBottom.current && !visible) node.scrollTop = node.scrollHeight; }, [state.mainMessages.length, visible]);
  const openAgent: OpenAgent = (id, trigger, preserveOpener = false) => {
    if (!agents.some(agent => agent.definition.id === id)) return;
    window.clearTimeout(replayTimer.current);
    if (!preserveOpener) opener.current = trigger;
    restoreOpener.current = true;
    if (!visible) mainPosition.current = mainScroll.current?.scrollTop ?? 0;
    const ancestors: AgentId[] = [];
    let parent = state.agents[id]?.definition.parentId;
    while (parent && state.agents[parent] && !ancestors.includes(parent)) { ancestors.push(parent); parent = state.agents[parent].definition.parentId; }
    ancestors.forEach((ancestorId, index) => { if (ancestors.length - index >= 2) onToggleBranch(ancestorId, true); });
    onSelect(id); setActive(id); setDrawerOpen(false); setPhase("open");
  };
  const close = (restore = true) => { restoreOpener.current = restore; setPhase("closing"); };
  const goMain = () => { setDrawerOpen(false); if (visible) close(); else mainScroll.current?.scrollTo({ top: mainScroll.current.scrollHeight, behavior: reduceMotion ? "instant" : "smooth" }); };
  const goOrigin = (id: string) => { setDrawerOpen(false); if (visible) { pendingOrigin.current = id; close(); } else scrollToOrigin(id); };
  const replay = (trigger: HTMLElement) => {
    const id = agents.find(agent => agent.definition.id === selected)?.definition.id ?? agents[0]?.definition.id;
    if (!id) return;
    opener.current = trigger; setPhase("closed"); setDrawerOpen(false);
    window.clearTimeout(replayTimer.current);
    replayTimer.current = window.setTimeout(() => openAgent(id, trigger, true), reduceMotion ? 0 : 330);
  };
  const rail = (mobile = false) => <TeamRail design={design} state={state} selected={visible ? active : MAIN_AGENT_ID} onOpen={openAgent} onMain={goMain} headingRef={teamHeading} onClose={mobile ? () => setDrawerOpen(false) : undefined} railVisible={!mobile || drawerOpen} acceptedSettings={acceptedSettings} savedSettings={savedSettings} onConfigure={onConfigure} arrivalTimes={arrivalTimes} treeLimit={treeLimit} onTreeLimit={onTreeLimit} expandedAgents={expandedAgents} onToggleBranch={onToggleBranch} />;
  const recipient = visible ? state.agents[active] : mainAgentSnapshot(state, acceptedSettings);
  const recipientIsMain = recipient.definition.id === MAIN_AGENT_ID;
  const sendToRecipient = (id: AgentId, value: string) => {
    if (id === MAIN_AGENT_ID) {
      onMainSend(value);
      window.requestAnimationFrame(() => { if (mainScroll.current) mainScroll.current.scrollTop = mainScroll.current.scrollHeight; });
    } else {
      messaging.onSend(id, value);
      if (filters[id] === "tools") setFilters(current => ({ ...current, [id]: "messages" }));
      setMessageScroll(value => value + 1);
    }
  };
  const groupsRendered = new Set<string>();
  const delegationOrigins = new Set(Object.values(state.agents).map(agent => agent.definition.originMessageId));
  return <div className={`app-preview preview-${design}${visible ? " has-agent-thread" : ""}`} onKeyDown={event => { if (event.key === "Escape" && phase === "open" && !drawerOpen && !(event.target instanceof Element && event.target.closest("dialog[open]"))) { event.preventDefault(); event.stopPropagation(); close(); } }}>
    <header className="app-header"><div><span className="session-breadcrumb">workspace<Glyph name="chevron" size={11} />Conversation</span><h2>Image experience</h2></div><div className="app-header-actions"><button type="button" className="replay-opening" disabled={!agents.length} onClick={event => replay(event.currentTarget)}><Glyph name="reset" size={13} /><span>Replay opening</span></button><button type="button" className="team-toggle" ref={teamToggle} aria-label={narrow ? "Open working team" : "Go to main conversation"} aria-expanded={narrow ? drawerOpen : undefined} onClick={() => narrow ? setDrawerOpen(true) : goMain()}><TeamIcon size={17} /><span>{narrow ? "Team" : "Main chat"}</span><span className="count-badge">{agents.length + 1}</span></button></div></header>
    <div className="hybrid-layout"><div className="chat-area"><div className="chat-stage">
      <div className={`conversation-scroll${visible ? " is-behind-focus" : ""}`} ref={mainScroll} onScroll={() => { const node = mainScroll.current; if (node) nearBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 70; }} aria-hidden={visible ? true : undefined} inert={visible}><div className="main-transcript"><MainConversationHeader state={state} acceptedSettings={acceptedSettings} savedSettings={savedSettings} onConfigure={onConfigure} />{state.mainMessages.map(message => {
        const origin = message.role === "assistant" && !message.returnedAgentId ? message.originMessageId : undefined;
        const showGroup = origin && delegationOrigins.has(origin) && !groupsRendered.has(origin);
        if (showGroup) groupsRendered.add(origin);
        return <MainMessageView key={message.id} message={message} state={state} onOpen={openAgent} acceptedSettings={acceptedSettings}>{showGroup && <AgentLifecycleView state={state} origin={origin} variant={design} arrivals={arrivalTimes} onOpen={openAgent} />}</MainMessageView>;
      })}{state.now === 0 && <div className="scenario-start"><Glyph name="play" size={14} /><p>Play the scenario to see this message become a delegation, a continuing conversation, and returned results.</p></div>}</div></div>
      {visible && <div className="focused-agent-thread"><div className={`thread-surface${phase === "closing" ? " is-closing" : ""}`} inert={phase === "closing"}><AgentThread design={design} state={state} agent={state.agents[active]} filter={filters[active] ?? "all"} onFilter={filter => setFilters(current => ({ ...current, [active]: filter }))} onClose={close} onOrigin={goOrigin} onOpen={openAgent} headingRef={heading} scrollPositions={threadPositions} acceptedSettings={acceptedSettings} savedSettings={savedSettings} onConfigure={onConfigure} scrollRequest={messageScroll} /></div></div>}
    </div><ConversationComposer agent={recipient} value={recipientIsMain ? mainDraft : messaging.drafts[recipient.definition.id] ?? ""} onChange={value => recipientIsMain ? onMainDraft(value) : messaging.onDraft(recipient.definition.id, value)} onSend={sendToRecipient} playing={messaging.playing} queued={recipientIsMain ? state.pendingMainReplies.length : queuedCount(recipient)} /></div>{!narrow && <aside className="right-workspace">{rail()}</aside>}</div>
    {narrow && <Drawer open={drawerOpen} label="Working team" onClose={() => setDrawerOpen(false)}>{rail(true)}</Drawer>}
  </div>;
}

export function App() {
  const hash = window.location.hash.slice(1);
  const [design, setDesign] = useState<Design>(designs.some(item => item.id === hash) ? hash as Design : "more");
  const [savedSettings, setSavedSettings] = useState<RuntimeSettingsMap>(createRuntimeSettings);
  const [acceptedSettings, setAcceptedSettings] = useState<RuntimeSettingsMap>(createRuntimeSettings);
  const [state, setState] = useState(() => applyLaunchSettings(startDemo(), createRuntimeSettings()));
  const [selected, setSelected] = useState<AgentId>("research");
  const [settingsAgent, setSettingsAgent] = useState<AgentId>("");
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [drafts, setDrafts] = useState<Record<AgentId, string>>({});
  const [mainDraft, setMainDraft] = useState("");
  const [resetKey, setResetKey] = useState(0);
  const { arrivalTimes, arrivalAnnouncement } = useAgentArrivals(state, resetKey);
  const [treeLimit, setTreeLimit] = useState(4);
  const [expandedAgents, setExpandedAgents] = useState<Record<AgentId, boolean>>({});
  const activeDesign = designs.find(item => item.id === design)!;
  const pendingCount = Object.keys(savedSettings).filter(id => settingsPending(id, savedSettings, acceptedSettings)).length;
  const ended = state.now >= DEMO_END && Object.keys(state.followUps).length === 0 && state.pendingMainReplies.length === 0;
  useEffect(() => { if (!playing || ended) return; const timer = window.setInterval(() => setState(current => advanceTo(current, current.now + 1)), 1000 / speed); return () => window.clearInterval(timer); }, [playing, speed, ended]);
  useEffect(() => { if (ended) setPlaying(false); }, [ended]);
  const launch = (clearDrafts: boolean) => {
    const snapshot = copyRuntimeSettings(savedSettings);
    setAcceptedSettings(snapshot); setState(applyLaunchSettings(startDemo(), snapshot));
    setSelected("research"); setSettingsAgent(""); setTreeLimit(4); setExpandedAgents({}); setResetKey(value => value + 1);
    if (clearDrafts) { setDrafts({}); setMainDraft(""); }
  };
  const reset = () => { launch(true); setPlaying(false); };
  const saveSettings = (settings: RuntimeSettings) => setSavedSettings(current => ({ ...current, [settingsAgent]: { ...settings } }));
  return <div className={`studio design-${design}${playing ? " is-playing" : " is-paused"}`}>
    <a className="skip-link" href="#design-preview">Skip to interactive design</a>
    <header className="studio-header"><div className="studio-brand"><span className="brand-mark"><TeamIcon size={25} /></span><div><span className="eyebrow">ngn / focused agent conversations</span><h1>One conversation at a time.</h1></div></div><div className="demo-label"><span className="revision-pill">Revision 08</span><span>Interactive study · simulated context</span></div></header>
    <div className="design-switcher" aria-label="Compare agent activity designs">{designs.map(item => <button type="button" key={item.id} className={`design-choice${design === item.id ? " active" : ""}`} aria-pressed={design === item.id} onClick={() => { setDesign(item.id); window.history.replaceState(null, "", `#${item.id}`); }}><span className="option-letter">{item.letter}</span><span><strong>{item.name}</strong><small>{item.description}</small></span></button>)}</div>
    <div className="study-utilities"><span className="chosen-team-mark"><TeamIcon size={17} />One team tree · one message box</span><span>{pendingCount ? `${pendingCount} agent${pendingCount === 1 ? "" : "s"} with settings saved for the next run` : "Context estimates and model windows are illustrative."}</span></div>
    <section className="simulation-controls" aria-label="Simulated event timeline"><div className="transport-buttons"><button type="button" className="play-button" onClick={() => { if (ended) launch(false); setPlaying(value => !value); }}><Glyph name={playing ? "pause" : "play"} size={14} />{playing ? "Pause" : ended ? "Replay scenario" : "Play scenario"}</button><button type="button" title="Advance to the next event" onClick={() => { setPlaying(false); setState(current => advanceTo(current, nextEventTime(current))); }}><Glyph name="next" size={14} /><span>Next event</span></button><button type="button" className="icon-button reset-button" aria-label="Reset scenario with saved settings and clear messages" title="Restart with saved settings" onClick={reset}><Glyph name="reset" size={16} /></button></div><div className="timeline-progress"><div><span>{playing ? "Scenario running" : ended ? "Scenario complete" : "Timeline paused"}</span><time>{time(state.now)} <span>/ {time(DEMO_END)}</span></time></div><progress value={Math.min(state.now, DEMO_END)} max={DEMO_END} aria-label="Demo timeline" /></div>{state.now < DEMO_END && <button type="button" className="jump-to-work" onClick={() => { setPlaying(false); setState(current => advanceTo(current, current.now < 2 ? 2 : current.now < 52 ? 52 : current.now < 60 ? 60 : DEMO_END)); }}>{state.now < 2 ? "Show first spawn" : state.now < 52 ? "Next delegation" : state.now < 60 ? "Show mixed states" : "Show completions"}<Glyph name="arrow" size={12} /></button>}<label className="speed-control">Speed<select value={speed} onChange={event => setSpeed(Number(event.target.value))}><option value={1}>1×</option><option value={2}>2×</option><option value={4}>4×</option></select></label></section>
    <div className="design-caption"><p><strong>{activeDesign.letter}.</strong> {activeDesign.detail}</p><span>No backend connection</span></div>
    <main id="design-preview" tabIndex={-1}><HybridView key={resetKey} design={design} state={state} selected={selected} onSelect={setSelected} onSend={(id, value) => setState(current => queueMessage(current, id, value))} playing={playing} drafts={drafts} onDraft={(id, value) => setDrafts(current => ({ ...current, [id]: value }))} mainDraft={mainDraft} onMainDraft={setMainDraft} onMainSend={value => setState(current => queueMainMessage(current, value))} acceptedSettings={acceptedSettings} savedSettings={savedSettings} onConfigure={setSettingsAgent} arrivalTimes={arrivalTimes} treeLimit={treeLimit} onTreeLimit={setTreeLimit} expandedAgents={expandedAgents} onToggleBranch={(id, expanded) => setExpandedAgents(current => ({ ...current, [id]: expanded }))} /></main>
    <footer className="studio-footer"><span>Newest branch activity first · two delegation levels expanded</span><span>Model context window ≠ configured compaction budget.</span></footer><p className="sr-only" role="status" aria-live="polite">{state.announcement}</p>
    <p className="sr-only" role="status" aria-live="polite">{arrivalAnnouncement}</p>
    {settingsAgent && <AgentSettingsDialog key={settingsAgent} agent={settingsAgent === MAIN_AGENT_ID ? mainAgentSnapshot(state, acceptedSettings) : state.agents[settingsAgent]} settings={savedSettings[settingsAgent] ?? {}} onSave={saveSettings} onClose={() => setSettingsAgent("")} />}
  </div>;
}
