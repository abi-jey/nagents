import { useEffect, useMemo, useRef } from "react";
import { Conversation } from "../chat/Conversation.js";
import { appendEvent, fromHistory, type Entry } from "../chat/transcript.js";
import { Icon } from "../../components/Icon.js";
import { ProviderLogo } from "../../components/ProviderLogo.js";
import { AgentState, type ArrivalTimes } from "./TeamTree.js";
import { TaskTimeline } from "./TaskTimeline.js";
import { mergeTaskActivity, providerLabel, type TaskActivity, type TaskDetail, type TeamAgent } from "./model.js";
import { isProfileTarget, type TeamController } from "./useTeam.js";
import type { WireEvent } from "../../types.js";
import type { ChatAppearance } from "../settings/appearance.js";

export function taskEntries(detail: TaskDetail | undefined, streamed: TaskActivity[]): Entry[] {
  if (!detail) return [];
  let entries = fromHistory({ session_id: detail.task.child_session_id, history: detail.history, retained_tasks: [] });
  const later = streamed.filter(event => event.sequence > (detail.activity_cursor ?? Number.MAX_SAFE_INTEGER));
  if (detail.activity_truncated || later.some(event => event.task_id === detail.task.id && event.payload.type === "resync_required")) return entries;
  for (const event of mergeTaskActivity(detail.activity || [], later)) {
    if (event.task_id !== detail.task.id) continue;
    const payload: WireEvent = { ...event.payload, event: event.payload.type };
    if (!["response_metadata", "text_chunk", "text_done", "tool_call_progress", "tool_call", "tool_execution_started", "tool_output", "tool_result", "status", "approval", "approval_closed"].includes(payload.event)) continue;
    if (payload.event === "response_metadata" && payload.response_session_id !== detail.task.child_session_id) continue;
    // This is a task-local transcript. Identity/recipient fields in provider data
    // cannot create a root entry, another agent, or an executable action.
    entries = appendEvent(entries, { ...payload, task_id: "", parent_task_id: "", run_id: "", activation: event.activation });
  }
  return entries;
}

export function TaskConversation({ team, agents, arrivals, token, sessionId, back, appearance }: {
  team: TeamController; agents: TeamAgent[]; arrivals: ArrivalTimes; token: string; sessionId: string; back: () => void;
  appearance?: ChatAppearance;
}) {
  const agent = agents.find(item => item.id === team.selected);
  const removedProfile = team.loaded && isProfileTarget(team.selected) && !team.profile;
  const name = agent?.name || team.profile?.name || (isProfileTarget(team.selected) ? team.selected.slice("profile:".length) : "Agent");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { const frame = requestAnimationFrame(() => heading.current?.focus({ preventScroll: true })); return () => cancelAnimationFrame(frame); }, [team.selected]);
  const entries = useMemo(() => taskEntries(team.detail, team.activity), [team.detail, team.activity]);
  return <section className="focused-task-conversation" aria-label={`${name} conversation`}>
    <header className="focused-task-header"><button type="button" className="task-back" onClick={back}><Icon name="chevron" size={14} />Assistant</button>
      <div><h2 ref={heading} tabIndex={-1}>{name}</h2><span className="focused-task-model">{agent && providerLabel(agent) && <ProviderLogo provider={agent.providerKind} />}{[agent ? providerLabel(agent) : team.profile?.provider, agent?.model || team.profile?.model].filter(Boolean).join(" · ")}</span></div>
      {agent && <AgentState agent={agent} />}
    </header>
    {removedProfile ? <p className="task-history-notice" role="status">This agent profile was removed or renamed. Choose an agent in Team or return to Assistant. Your draft is kept.</p>
      : team.profile ? <div className="team-profile-introduction"><span className="team-state state-ready">{team.startNotice ? "Starting" : "Not started"}</span><p>{team.profile.description || `Start a conversation with ${team.profile.name}.`}</p><p className="team-profile-help" role={team.startNotice ? "status" : undefined}>{team.startNotice || "Send a message below to assign its first task."}</p></div> : <>
      {team.loading && !team.detail && <p className="task-loading" role="status">Loading conversation…</p>}
      {(team.detail?.truncated || team.detail?.history_truncated) && <p className="task-history-notice">Some recorded content was shortened for this view.</p>}
      {(team.detail?.activity_truncated || team.activity.some(event => event.sequence > (team.detail?.activity_cursor || 0) && event.payload.type === "resync_required")) && <p className="task-history-notice" role="status">Some live updates were interrupted. Recorded activity will refresh when this response is saved.</p>}
      {team.detail?.before && <button type="button" className="task-load-earlier" disabled={team.loadingEarlier} onClick={() => void team.loadEarlier()}>{team.loadingEarlier ? "Loading…" : "Load earlier messages"}</button>}
      {agent?.prompt && <details className="task-assignment"><summary>Assigned task</summary><p>{agent.prompt}</p></details>}
      <Conversation appearance={appearance} key={team.selected} entries={entries} token={token} sessionId={sessionId} demo={false} canSubmit={false} submit={() => {}}
        renderTask={thread => agents.some(item => item.id === thread.id) ? <TaskTimeline id={thread.id} agents={agents} inspect={team.select} arrivals={arrivals} /> : undefined} />
      {!!team.detail?.receipts?.length && <div className="task-message-receipts" aria-label="Message delivery">
        {team.detail.receipts.filter(receipt => receipt.status !== "picked_up").slice(-6).map(receipt => <div key={receipt.message_id} className={`task-message-receipt receipt-${receipt.status}`}>
          <span>{receipt.status === "queued" ? "Queued for the next response" : receipt.status === "error" ? "Not delivered" : "Cancelled"}</span>
          {receipt.text && <p>{receipt.text}</p>}{receipt.reason && <small>{receipt.reason}</small>}
        </div>)}
      </div>}
    </>}
  </section>;
}
