import { useMemo } from "react";
import type { CSSProperties } from "react";
import { resolveAgentLifecycle } from "./agentLifecycle";
import type { AgentLifecycle } from "./agentLifecycle";
import { visibleAgents } from "./engine";
import type { ActivityState, AgentId, AgentRun } from "./types";
import "./lifecycle-chat.css";

export type ArrivalTimes = Record<AgentId, number>;
export function useArrivalStyle(at = 0): CSSProperties {
  return useMemo(() => at ? { "--arrival-delay": `${Math.min(0, at - Date.now())}ms` } as CSSProperties : {}, [at]);
}
function clock(at: number) { return `${Math.floor(at / 60)}:${String(at % 60).padStart(2, "0")}`; }

export function LifecycleStatus({ lifecycle }: { lifecycle: AgentLifecycle }) {
  return <span className={`lifecycle-state lifecycle-${lifecycle.state}`}><span className="lifecycle-mark" aria-hidden="true">{lifecycle.state === "running" ? <><i /><i /><i /></> : lifecycle.state === "done" ? "✓" : lifecycle.state === "failed" ? "!" : <span />}</span>{lifecycle.label}</span>;
}

/** Each item is a real agent record; spawn facts never stand in for current state. */
export function AgentLifecycleView({ state, origin, variant, arrivals, onOpen }: {
  state: ActivityState;
  origin: string;
  variant: "more" | "drawer";
  arrivals: ArrivalTimes;
  onOpen: (id: AgentId, trigger: HTMLElement) => void;
}) {
  const agents = visibleAgents(state).filter(agent => agent.definition.originMessageId === origin);
  if (!agents.length) return <p className="spawn-pending">Preparing agent tasks…</p>;
  return <section className={`agent-lifecycle-block compact-lifecycle-chat lifecycle-view-${variant}`} aria-label="Agent tasks and current states" data-origin={origin}>
    <ol className="agent-lifecycle-list">{agents.map(agent => <LifecycleItem key={agent.definition.id} agent={agent} state={state} variant={variant} arrivalAt={arrivals[agent.definition.id] ?? 0} onOpen={onOpen} />)}</ol>
  </section>;
}

function LifecycleItem({ agent, state, variant, arrivalAt, onOpen }: { agent: AgentRun; state: ActivityState; variant: "more" | "drawer"; arrivalAt: number; onOpen: (id: AgentId, trigger: HTMLElement) => void }) {
  const lifecycle = resolveAgentLifecycle(agent, state);
  const style = useArrivalStyle(arrivalAt);
  const detail = lifecycle.state === "running" && lifecycle.detail === agent.definition.role ? `Working on ${agent.definition.role.toLowerCase()}` : lifecycle.detail;
  const spawn = `${lifecycle.originLabel} at ${clock(lifecycle.startedAt)}`;
  const nested = agent.definition.parentId !== "assistant";
  const queued = lifecycle.queuedMessages ? `${lifecycle.queuedMessages} ${lifecycle.queuedMessages === 1 ? "message" : "messages"} queued` : "";
  const description = `${agent.definition.role}. ${spawn}. ${lifecycle.label}: ${detail}${queued ? `. ${queued}` : ""}`;
  return <li data-lifecycle-agent={agent.definition.id} data-lifecycle-state={lifecycle.state} className={`${nested ? "is-child-task " : ""}${arrivalAt ? "agent-arriving" : ""}`} style={style}>
    <button type="button" className="lifecycle-agent-open" aria-label={`Inspect & message ${agent.definition.name}. ${description}`} title={description} onClick={event => onOpen(agent.definition.id, event.currentTarget)}>
      {variant === "drawer" && <time className="lifecycle-spawn-clock" aria-label={`Spawned at ${clock(lifecycle.startedAt)}`}>{clock(lifecycle.startedAt)}</time>}
      <span className="lifecycle-identity"><strong>{agent.definition.name}</strong><span className="lifecycle-task">{agent.definition.role}</span></span>
      <LifecycleStatus lifecycle={lifecycle} />
      <span className="lifecycle-spawn" title={spawn}><span className="sr-only">Spawned </span>by {lifecycle.parentName}{variant === "more" && <time>{clock(lifecycle.startedAt)}</time>}</span>
      {lifecycle.state !== "done" && <span className="lifecycle-current-detail"><span>{detail}</span>{queued && <small>{queued}</small>}</span>}
    </button>
  </li>;
}
