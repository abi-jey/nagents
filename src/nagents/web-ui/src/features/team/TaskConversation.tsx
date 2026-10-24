import { useEffect, useMemo, useRef } from "react";
import { Conversation } from "../chat/Conversation.js";
import { appendEvent, fromHistory, type Entry } from "../chat/transcript.js";
import { reconcileHistory } from "../chat/liveTranscript.js";
import { Icon } from "../../components/Icon.js";
import { ProviderLogo } from "../../components/ProviderLogo.js";
import { AgentState, type ArrivalTimes } from "./TeamTree.js";
import { MAIN_AGENT, mergeTaskActivity, providerLabel, type TaskActivity, type TaskDetail, type TeamAgent } from "./model.js";
import { isProfileTarget, profileSelection, type TeamController } from "./useTeam.js";
import type { Snapshot, WireEvent } from "../../types.js";
import type { ChatAppearance } from "../settings/appearance.js";
import { RemoteStatus } from "./RemoteStatus.js";

function taskRecords(detail: TaskDetail, streamed: TaskActivity[]): TaskActivity[] {
  const later = streamed.filter(event => event.sequence > (detail.activity_cursor ?? Number.MAX_SAFE_INTEGER));
  if (detail.activity_truncated || later.some(event => event.session_id === detail.task.session_id && event.payload.type === "resync_required")) return [];
  return mergeTaskActivity(detail.activity || [], later).filter(event => event.session_id === detail.task.session_id);
}

function appendTaskRecord(entries: Entry[], event: TaskActivity, childSession: string): Entry[] {
  const payload: WireEvent = { ...event.payload, event: event.payload.type };
  if (payload.event === "status" && payload.phase === "failed" && typeof payload.detail === "string") {
    payload.event = "error"; payload.message = payload.detail;
  }
  if (!["response_metadata", "text_chunk", "text_done", "tool_call_progress", "tool_call", "tool_execution_started", "tool_output", "tool_result", "status", "error", "approval", "approval_closed"].includes(payload.event)) return entries;
  if (payload.event === "response_metadata" && payload.response_session_id !== childSession) return entries;
  // The envelope activation scopes this selected conversation. Enriched
  // metadata can also carry a root followup counter that text chunks omit;
  // mixing those scopes would split one answer into two visible entries.
  // Provider payloads cannot change the selected task or create a root action.
  return appendEvent(entries, { ...payload, session_id: "", parent_session_id: "", run_id: "", activation: event.activation, followup: 0 });
}

export function taskEntries(detail: TaskDetail | undefined, streamed: TaskActivity[]): Entry[] {
  if (!detail) return [];
  let entries = fromHistory({ session_id: detail.task.session_id, root_session_id: detail.task.root_session_id, history: detail.history, retained_tasks: [], retained_activations: detail.retained_activations, retained_activations_omitted: detail.retained_activations_omitted });
  for (const event of taskRecords(detail, streamed)) entries = appendTaskRecord(entries, event, detail.task.session_id);
  return entries;
}

/** Reconcile a selected child's polls in place, applying each live event once. */
export class TaskTranscript {
  generation = 0;
  private entries: Entry[] = [];
  private history: Snapshot["history"] | undefined;
  private applied = new Set<string>();

  update(detail: TaskDetail | undefined, streamed: TaskActivity[]): Entry[] {
    if (!detail) return this.entries;
    const records = taskRecords(detail, streamed);
    if (!this.history) this.entries = taskEntries(detail, streamed);
    else {
      const cleared = this.history.length > 0 && detail.history.length === 0;
      if (cleared) { this.entries = []; this.generation++; }
      for (const event of records) if (!this.applied.has(`${event.activation}:${event.sequence}`))
        this.entries = appendTaskRecord(this.entries, event, detail.task.session_id);
      this.entries = reconcileHistory(this.entries,
        { session_id: detail.task.session_id, root_session_id: detail.task.root_session_id, history: detail.history, retained_tasks: [], retained_activations: detail.retained_activations, retained_activations_omitted: detail.retained_activations_omitted, sessions: [] }, false, this.history, new Set(), !detail.activity_truncated);
    }
    for (const event of records) this.applied.add(`${event.activation}:${event.sequence}`);
    this.history = detail.history;
    return this.entries;
  }
}

export function TaskConversation({ team, agents, token, sessionId, back, appearance, configureConnection, activity = [] }: {
  team: TeamController; agents: TeamAgent[]; arrivals: ArrivalTimes; token: string; sessionId: string; back: () => void;
  appearance?: ChatAppearance;
  activity?: TaskActivity[];
  configureConnection?: (name: string) => void;
}) {
  const agent = agents.find(item => item.id === team.selected);
  const removedProfile = team.loaded && isProfileTarget(team.selected) && !team.profile;
  const name = agent?.name || team.profile?.name || (isProfileTarget(team.selected) ? profileSelection(team.selected).profile : "Agent");
  const rootName = agents.find(item => item.id === MAIN_AGENT)?.name || "Assistant";
  const parentName = agents.find(item => item.id === (agent?.parentId ?? MAIN_AGENT))?.name || rootName;
  const definition = agent?.profile || team.profile?.id || "";
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { const frame = requestAnimationFrame(() => heading.current?.focus({ preventScroll: true })); return () => cancelAnimationFrame(frame); }, [team.selected]);
  const transcript = useMemo(() => new TaskTranscript(), [sessionId, team.selected]);
  const entries = useMemo(() => transcript.update(team.detail, team.activity), [transcript, team.detail, team.activity]);
  const failure = team.detail?.task.status === "failed" ? team.detail.task.error : "";
  const authentication = agent?.authentication || team.profile?.authentication;
  const remote = agent?.providerKind === "a2a" || team.profile?.provider === "a2a";
  return <section className="focused-task-conversation" aria-label={`${name} conversation`}>
    <header className="focused-task-header"><button type="button" className="task-back" onClick={back}><Icon name="chevron" size={14} />{rootName}</button>
      <div><h2 ref={heading} tabIndex={-1}>{name}</h2><span className="focused-task-model">{agent?.id && <><code>{agent.id}</code><span aria-hidden="true"> · </span></>}{definition && <span className="focused-task-definition">Definition <b>{definition}</b><span aria-hidden="true"> · </span></span>}{agent && providerLabel(agent) && <ProviderLogo provider={agent.providerKind} />}{[agent ? providerLabel(agent) : team.profile?.provider, agent?.model || team.profile?.model].filter(Boolean).join(" · ")}</span></div>
      {agent && <AgentState agent={agent} />}
    </header>
    {remote && <div className="task-remote-connection"><RemoteStatus authentication={authentication} /><span>{agent?.trustRemoteTools || team.profile?.trust_remote_tools ? "Remote tools trusted" : "Remote tools reviewed"}</span>
      {configureConnection && definition && <button type="button" onClick={() => configureConnection(definition)}>Connection settings</button>}
    </div>}
    {removedProfile ? <p className="task-history-notice" role="status">This agent profile was removed or renamed. Choose an agent in Team or return to {rootName}. Your draft is kept.</p>
      : team.profile ? <div className="team-profile-introduction"><span className="team-state state-ready">{team.startNotice ? "Starting" : "Agent definition"}</span><p>{team.profile.description || `Start a conversation with ${team.profile.name}.`}</p><p className="team-profile-help" role={team.startNotice ? "status" : undefined}>{team.startNotice || (team.startParentId ? `Send a message below to start this child under ${(team.startParent?.title || team.startParent?.session_id) || "the selected parent"}.` : "Send a message below to start a new conversation using this definition.")}</p>
        {team.profile.source && <dl className="team-profile-source"><dt>Definition file</dt><dd><code>{team.profile.source}</code></dd></dl>}
      </div> : <>
      {team.loading && !team.detail && <p className="task-loading" role="status">Loading conversation…</p>}
      {(team.detail?.truncated || team.detail?.history_truncated) && <p className="task-history-notice">Some recorded content was shortened for this view.</p>}
      {(team.detail?.activity_truncated || team.activity.some(event => event.sequence > (team.detail?.activity_cursor || 0) && event.payload.type === "resync_required")) && <p className="task-history-notice" role="status">Some live updates were interrupted. Recorded activity will refresh when this response is saved.</p>}
      {team.detail?.before && <button type="button" className="task-load-earlier" disabled={team.loadingEarlier} onClick={() => void team.loadEarlier()}>{team.loadingEarlier ? "Loading…" : "Load earlier messages"}</button>}
      {agent?.prompt && <details className="task-assignment"><summary>Assigned task</summary><p>{agent.prompt}</p></details>}
      {failure && !entries.some(entry => entry.kind === "error") && <div className="task-failure" role="alert"><strong>Request failed</strong><p>{failure}</p></div>}
      <Conversation appearance={appearance} agentName={name} parentAgentName={parentName} key={`${team.selected}:${transcript.generation}`} entries={entries} token={token} sessionId={sessionId} demo={false} canSubmit={false} submit={() => {}}
        delegation={{ agents, activity, parentId: team.selected, inspect: team.select }} />
      {!!team.detail?.receipts?.length && <div className="task-message-receipts" aria-label="Message delivery">
        {team.detail.receipts.filter(receipt => receipt.status !== "picked_up").slice(-6).map(receipt => <div key={receipt.message_id} className={`task-message-receipt receipt-${receipt.status}`}>
          <span>{receipt.status === "queued" ? "Queued for the next response" : receipt.status === "error" ? "Not delivered" : "Cancelled"}</span>
          {receipt.text && <p>{receipt.text}</p>}{receipt.reason && <small>{receipt.reason}</small>}
        </div>)}
      </div>}
    </>}
  </section>;
}
