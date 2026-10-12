import { useState } from "react";
import { Icon } from "../../components/Icon.js";
import { AgentState, useArrivalStyle, type ArrivalTimes } from "./TeamTree.js";
import { orderedTeam, type TeamAgent } from "./model.js";

export function TaskTimeline({ id, agents, inspect, arrivals }: {
  id: string; agents: TeamAgent[]; inspect: (id: string) => void; arrivals: ArrivalTimes;
}) {
  const { children } = orderedTeam(agents);
  const [expanded, setExpanded] = useState(new Set<string>());
  const agent = agents.find(item => item.id === id);
  if (!agent) return null;
  return <ol className="task-timeline" aria-label={`${agent.name} delegation`} data-record-key={`task:${id}`} id={`task-${id}`}><TimelineNode item={agent} agents={agents} childrenByParent={children} depth={0} expanded={expanded} expand={id => setExpanded(current => new Set([...current, id]))} inspect={inspect} arrivals={arrivals} /></ol>;
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
      {terminal ? <div className="task-timeline-settled"><details className="task-timeline-result"><summary className="task-timeline-row is-settled">
        <span className="task-timeline-dot" aria-hidden="true" /><strong>{item.name}</strong><AgentState agent={item} /><span className="task-timeline-result-preview">{item.reason}</span>{spawn}
      </summary><p>{item.reason || "No result text was recorded."}</p></details><button type="button" className="task-timeline-inspect" onClick={() => inspect(item.id)} aria-label={`Open ${item.name} conversation`}><Icon name="chat" size={13} /></button></div> : <div className="task-timeline-row">
        <span className="task-timeline-dot" aria-hidden="true" />
        <button type="button" className="task-timeline-open" onClick={() => inspect(item.id)} aria-label={`Inspect and message ${item.name}`}>
          <strong>{item.name}</strong><AgentState agent={item} />
          <span className="task-timeline-task">{item.prompt}</span>
        </button>
        {spawn}
        <button type="button" className="task-timeline-inspect" onClick={() => inspect(item.id)} aria-label={`Open ${item.name} conversation`}><Icon name="chat" size={13} /></button>
      </div>}
      {!!descendants.length && (open ? <ol>{descendants.map(child => <TimelineNode key={child.id} item={child} agents={agents} childrenByParent={childrenByParent} depth={depth + 1} expanded={expanded} expand={expand} inspect={inspect} arrivals={arrivals} />)}</ol>
        : <button type="button" className="task-timeline-more" onClick={() => expand(item.id)}>{descendants.length} nested agent{descendants.length === 1 ? "" : "s"}</button>)}
    </li>;
}
