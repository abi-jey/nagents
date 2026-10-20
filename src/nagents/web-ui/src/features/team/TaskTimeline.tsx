import { useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { AgentState, useArrivalStyle, type ArrivalTimes } from "./TeamTree.js";
import { orderedTeam, type TeamAgent } from "./model.js";
import type { TaskThread } from "../chat/transcript.js";

function observedAgents(thread: TaskThread): TeamAgent[] {
  const evidence = thread.items.flatMap(item => item.kind === "entry" && item.entry.kind === "task" ? [item.entry] : []).at(-1);
  const state = thread.state.toLowerCase();
  const phase = state.includes("failed") ? "failed" : state.includes("cancelled") ? "cancelled"
    : /completed|done|returned/.test(state) ? "done" : state.includes("waiting") ? "waiting" : "running";
  return [{ id: thread.id, name: thread.name, profile: "", parentId: thread.parentId || "", prompt: evidence?.inputs || "",
    phase, reason: phase === "done" ? evidence?.result || "" : phase === "failed" ? evidence?.error || "" : phase === "waiting" ? thread.state : "",
    provider: "", providerKind: "", model: "", startedAt: 0, updatedAt: 0, canMessage: false, messageReason: "" },
    ...thread.items.flatMap(item => item.kind === "thread" ? observedAgents(item.thread) : [])];
}

export function TaskTimeline({ id, agents, inspect, arrivals, thread }: {
  id: string; agents: TeamAgent[]; inspect: (id: string) => void; arrivals: ArrivalTimes; thread?: TaskThread;
}) {
  const combined = new Map((thread ? observedAgents(thread) : []).map(agent => [agent.id, agent]));
  for (const agent of agents) combined.set(agent.id, agent);
  const observed = [...combined.values()];
  const { children } = orderedTeam(observed);
  const ordering = useRef(new Map<string, string[]>());
  for (const [parent, descendants] of children) {
    const order = ordering.current.get(parent) || [];
    for (const child of descendants) if (!order.includes(child.id)) order.push(child.id);
    ordering.current.set(parent, order);
    descendants.sort((left, right) => order.indexOf(left.id) - order.indexOf(right.id));
  }
  const [expanded, setExpanded] = useState(new Set<string>());
  const agent = combined.get(id);
  if (!agent) return null;
  return <ol className="task-timeline" aria-label={`${agent.name} delegation`} data-record-key={`task:${id}`} id={`task-${id}`}><TimelineNode item={agent} agents={observed} childrenByParent={children} depth={0} expanded={expanded} expand={id => setExpanded(current => new Set([...current, id]))} inspect={inspect} arrivals={arrivals} /></ol>;
}

function TimelineNode({ item, agents, childrenByParent, depth, expanded, expand, inspect, arrivals }: {
  item: TeamAgent; agents: TeamAgent[]; childrenByParent: Map<string, TeamAgent[]>; depth: number; expanded: Set<string>;
  expand: (id: string) => void; inspect: (id: string) => void; arrivals: ArrivalTimes;
}) {
    const motion = useArrivalStyle(arrivals[item.id]);
    const descendants = childrenByParent.get(item.id) || [];
    const parent = agents.find(candidate => candidate.id === item.parentId);
    const time = item.startedAt ? new Date(item.startedAt * 1000) : undefined;
    const terminal = ["done", "failed", "cancelled"].includes(item.phase);
    const open = depth < 2 || expanded.has(item.id);
    const spawn = <span className="task-timeline-spawn" title={`Spawned by ${parent?.name || "Assistant"}${time ? ` at ${time.toLocaleString()}` : ""}`}><span className="sr-only">Spawned by </span>{parent?.name || "Assistant"}{time && <time dateTime={time.toISOString()}>{time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>}</span>;
    return <li data-task-id={item.id} className={`task-timeline-node${arrivals[item.id] ? " is-arriving" : ""}`} style={motion}>
      <div className="task-timeline-settled"><details className="task-timeline-result" data-state={item.phase}><summary className={`task-timeline-row${terminal ? " is-settled" : ""}`}>
        <span className="task-timeline-dot" aria-hidden="true" />
        <button type="button" className="task-timeline-open" onClick={event => { event.preventDefault(); inspect(item.id); }} aria-label={`Inspect and message ${item.name}`}>
          <strong>{item.name}</strong><AgentState agent={item} />
          <span className="task-timeline-task">{terminal ? item.reason : item.prompt}</span>
        </button>
        {spawn}
      </summary><p>{terminal ? item.reason || "No result text was recorded." : item.prompt}</p></details>
        <button type="button" className="task-timeline-inspect" onClick={() => inspect(item.id)} aria-label={`Open ${item.name} conversation`}><Icon name="chat" size={13} /></button>
      </div>
      {!!descendants.length && (open ? <ol>{descendants.map(child => <TimelineNode key={child.id} item={child} agents={agents} childrenByParent={childrenByParent} depth={depth + 1} expanded={expanded} expand={expand} inspect={inspect} arrivals={arrivals} />)}</ol>
        : <button type="button" className="task-timeline-more" onClick={() => expand(item.id)}>{descendants.length} nested agent{descendants.length === 1 ? "" : "s"}</button>)}
    </li>;
}
