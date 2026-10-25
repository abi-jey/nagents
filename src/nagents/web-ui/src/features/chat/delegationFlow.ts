import { agentUpdates } from "./backgroundUpdates.js";
import { appendEvent, type Entry, type ResponseReference } from "./transcript.js";
import { profileDescription, profileLabel, type AgentPhase, type TaskActivity, type TeamAgent, type TeamProfile } from "../team/model.js";

export type DelegationPresentation = {
  kind: "start" | "result"; detailsTruncated?: boolean; taskId: string; name: string; profile: string;
  phase: AgentPhase; provider?: string; model?: string; activation?: number; accepted?: boolean; prompt: string; text: string; latest?: Entry; lastTool?: Entry; sources: Entry[];
  profileName?: string; profileDescription?: string;
};
export type DelegationOptions = {
  agents: readonly TeamAgent[]; activity: readonly TaskActivity[]; parentId?: string;
  profiles?: readonly TeamProfile[];
  inspect: (id: string) => void;
};

function argumentsOf(entry: Entry): Record<string, unknown> {
  try { const value: unknown = JSON.parse(entry.inputs || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  } catch { return {}; }
}

function savedTasks(entry: Entry): string[] {
  if (entry.kind !== "context") return [];
  const updates = agentUpdates(entry.text);
  return updates && !updates.human_messages.length && !updates.more ? updates.sessions.map(session => session.session_id) : [];
}

function ownActivity(events: readonly TaskActivity[], taskId: string, activation: number): Entry[] {
  let entries: Entry[] = [];
  for (const event of events) {
    if (event.session_id !== taskId || event.activation !== activation ||
      !["response_metadata", "text_chunk", "text_done", "tool_call_progress", "tool_call", "tool_execution_started", "tool_output", "tool_result", "error", "approval", "approval_closed"].includes(event.payload.type)) continue;
    entries = appendEvent(entries, { ...event.payload, event: event.payload.type,
      session_id: "", parent_session_id: "", run_id: "", activation, followup: 0 });
  }
  return entries;
}

function terminal(entry: Entry | undefined): boolean {
  if (!entry) return false;
  if (entry.recorded) return ["completed", "failed", "cancelled"].includes(entry.recordedStatus || "");
  return entry.result !== undefined || ["Completed", "Error", "Cancelled"].includes(entry.state || "");
}

type Binding = { taskId: string; activation: number };
type Finished = {
  anchor: string; offset: number; phase: AgentPhase; text: string; name: string; profile: string; prompt: string;
  latest?: Entry; lastTool?: Entry; provider?: string; model?: string; detailsTruncated?: boolean; observed: boolean;
  generation?: ResponseReference; generationSettled: boolean; order: number;
};
type PositionedEntry = { entry: Entry; position: number; order?: number };

function generationBoundary(entries: readonly Entry[], generation: ResponseReference): { pending: boolean; anchor?: string } {
  const responses = entries.filter(entry => entry.kind === "assistant" && entry.responseId === generation.responseId &&
    entry.responseMetadata?.response_session_id === generation.sessionId);
  const current = responses.reduce<Entry | undefined>((latest, entry) =>
    (entry.responseMetadata?.revision || 0) >= (latest?.responseMetadata?.revision || 0) ? entry : latest, undefined);
  const metadata = current?.responseMetadata;
  if (!metadata || !["completed", "failed", "cancelled"].includes(metadata.status)) return { pending: true };
  // A generation can produce both prose and tool calls. Their committed
  // assistant history ID (or live response owner) identifies the same boundary
  // even when a checkpoint replaces the original metadata placeholder's ID.
  const slots = entries.filter(entry => entry.kind === "assistant" && entry.responseId === generation.responseId &&
      (entry.taskId || "") === (current?.taskId || "") || entry.kind === "tool" &&
      (entry.responseOwner?.responseId === generation.responseId && entry.responseOwner.sessionId === generation.sessionId ||
        !!metadata.history_id && entry.historyId === metadata.history_id && (entry.taskId || "") === (current?.taskId || "")));
  return { pending: false, anchor: slots.at(-1)?.id || current?.id };
}

function matchesActivation(call: Entry, lifecycle: Entry): boolean {
  return !!lifecycle.activationHistoryId && !!lifecycle.activationCallId &&
    lifecycle.activationCallPosition !== undefined && lifecycle.activationCallPosition >= 0 &&
    call.historyId === lifecycle.activationHistoryId && call.callId === lifecycle.activationCallId &&
    call.callPosition === lifecycle.activationCallPosition &&
    (!call.runId || call.runId === lifecycle.activationRunId);
}

/** Presentation only. Execution, approvals, model inputs and source records remain unchanged. */
export class DelegationFlow {
  private finished = new Map<string, Finished>();
  private bindings = new Map<string, Binding>();
  private visibleEvidence = new Set<string>();
  private activitySource?: readonly TaskActivity[];
  private activityCache = new Map<string, Entry[]>();

  project(entries: Entry[], options: DelegationOptions): Entry[] {
    if (!entries.length) { this.finished.clear(); this.bindings.clear(); this.visibleEvidence.clear(); return entries; }
    if (this.activitySource !== options.activity) {
      this.activitySource = options.activity; this.activityCache.clear();
    }
    const parentId = options.parentId || "";
    const known = new Map(options.agents.filter(agent => agent.id).map(agent => [agent.id, agent]));
    const positions = new Map(entries.map((entry, index) => [entry.id, index]));
    const lifecycleEntries = entries.filter(entry => entry.kind === "task");
    // These saved messages locate an already verified host completion; they
    // never establish its status, result, identity or activation.
    const contextSlots = entries.flatMap(entry => (agentUpdates(entry.text)?.sessions || [])
      .map((update, index, updates) => ({ entry, update, key: `${entry.id}:${index}`, offset: .25 + index / (updates.length + 1) * .25 })));
    const anchoredContexts = new Set<string>();
    // Partial JSON must not change a canonical request's presentation branch.
    const calls = entries.filter(entry => entry.kind === "tool" && entry.title === "to_agent" &&
      !entry.remoteReview && (!(entry.taskId || "") || entry.taskId === parentId) &&
      !("remote_operation" in argumentsOf(entry)));
    const claimed = new Set([...this.bindings.values()].map(binding => `${binding.taskId}:${binding.activation}`));
    const items = calls.map(call => {
      const args = argumentsOf(call);
      let savedId = "";
      try {
        const acknowledgement: unknown = JSON.parse(call.result || "{}");
        if (acknowledgement && typeof acknowledgement === "object" && "session_id" in acknowledgement && typeof acknowledgement.session_id === "string") savedId = acknowledgement.session_id;
      } catch { /* Incomplete acknowledgements do not establish identity. */ }
      const candidate = call.delegatedTaskId || savedId || (typeof args.session_id === "string" ? args.session_id : "");
      const taskId = candidate && (call.delegatedTaskId || known.has(candidate) || lifecycleEntries.some(entry => entry.taskId === candidate)) ? candidate : "";
      let binding = this.bindings.get(call.id);
      if (!binding && !call.error) {
        const exact = lifecycleEntries.find(entry => (!taskId || entry.taskId === taskId) && matchesActivation(call, entry));
        // New-session requests can own activation zero. Resumes require their
        // own host provenance and cannot borrow the previous completion.
        const initial = taskId && !args.session_id && (call.delegatedTaskId || savedId)
          ? lifecycleEntries.find(entry => entry.taskId === taskId && (entry.activation || 0) === 0 && !entry.activationCallId)
          : undefined;
        const matched = exact || initial;
        const initialAgent = taskId && !args.session_id && call.delegatedTaskId && !known.get(taskId)?.task?.activation;
        const activation = matched?.activation || 0;
        const id = matched?.taskId || (initialAgent ? taskId : "");
        if (id && !claimed.has(`${id}:${activation}`)) {
          binding = { taskId: id, activation };
          this.bindings.set(call.id, binding); claimed.add(`${id}:${activation}`);
        }
      }
      const target = binding?.taskId || taskId;
      const agent = known.get(target);
      const evidence = binding ? lifecycleEntries.filter(entry => entry.taskId === binding.taskId && (entry.activation || 0) === binding.activation) : [];
      const lifecycle = evidence.find(entry => !entry.recorded) || evidence[0];
      const key = binding ? `${binding.taskId}:${binding.activation}` : "";
      const direct = !agent || agent.parentId === parentId;
      let local: Entry[] = [];
      if (binding && direct) {
        local = this.activityCache.get(key) || ownActivity(options.activity, binding.taskId, binding.activation);
        this.activityCache.set(key, local);
      }
      const latest = local.findLast(entry => entry.kind === "tool" || entry.kind === "assistant" && !!(entry.text || entry.responseMetadata?.summary));
      const lastTool = local.findLast(entry => entry.kind === "tool");
      const polledPhase = binding && (!agent?.task || agent.task.activation === binding.activation) ? agent?.phase || "running" : "running";
      // Registry polling can overtake this activation's completion event. Only
      // its own terminal lifecycle may create/freeze a result row.
      const activePhase = ["done", "failed", "cancelled"].includes(polledPhase) ? "running" : polledPhase;
      const phase: AgentPhase = call.error ? "failed" : terminal(lifecycle)
        ? lifecycle?.error || lifecycle?.state === "Error" || lifecycle?.recordedStatus === "failed" ? "failed" : lifecycle?.state === "Cancelled" || lifecycle?.recordedStatus === "cancelled" ? "cancelled" : "done"
        : ["Waiting for approval", "Approval required"].includes(call.state || "") ? "waiting"
          : activePhase;
      const name = lifecycle?.title || agent?.name || target;
      const profile = lifecycle?.taskProfile !== undefined ? lifecycle.taskProfile || "Agent"
        : typeof args.agent === "string" && args.agent ? args.agent : agent?.profile || "Agent";
      const provider = lifecycle?.recorded ? lifecycle.taskProvider || "" : lifecycle?.taskProvider ?? agent?.provider;
      const model = lifecycle?.recorded ? lifecycle.taskModel || "" : lifecycle?.taskModel ?? agent?.model;
      const prompt = typeof args.prompt === "string" ? args.prompt : lifecycle?.inputs || agent?.prompt || "";
      const result = lifecycle?.error || lifecycle?.result || "";
      const context = binding && terminal(lifecycle) ? contextSlots.find(slot => !anchoredContexts.has(slot.key) &&
        slot.update.session_id === binding.taskId && slot.update.status === (phase === "done" ? "completed" : phase) &&
        positions.get(slot.entry.id)! > positions.get(call.id)! && ((slot.update.error ?? slot.update.result) === result ||
          !!slot.update.truncated && result.startsWith(slot.update.error ?? slot.update.result ?? "") ||
          !!lifecycle?.detailsTruncated && (slot.update.error ?? slot.update.result ?? "").startsWith(result))) : undefined;
      if (context) anchoredContexts.add(context.key);
      if (binding && direct && terminal(lifecycle) && ["done", "failed", "cancelled"].includes(phase) && !this.finished.has(key)) {
        const generation = lifecycle?.completionGeneration;
        this.finished.set(key, { anchor: lifecycle?.completionAfterId || context?.entry.id || entries.at(-1)?.id || call.id,
          offset: .25, phase, text: result, name, profile, prompt, latest, lastTool, provider, model, detailsTruncated: lifecycle?.detailsTruncated,
          observed: !!lifecycle && !lifecycle.recorded, generation, generationSettled: !generation,
          order: lifecycle?.completedAt || lifecycle?.activity || 0 });
      }
      const finished = this.finished.get(key);
      if (finished?.generation) {
        const boundary = generationBoundary(entries, finished.generation);
        if (!boundary.pending) {
          finished.generationSettled = true;
          if (boundary.anchor) { finished.anchor = boundary.anchor; finished.offset = .25; }
        }
      } else if (finished && lifecycle?.completionAfterId && positions.has(lifecycle.completionAfterId)) {
        finished.anchor = lifecycle.completionAfterId; finished.offset = .25;
      } else if (finished && !finished.observed && context) {
        // A cold completion batch retains its saved delivery order, even when
        // the children were started in a different order. Live anchors stay put.
        finished.anchor = context.entry.id; finished.offset = context.offset;
      }
      return { call, binding, target, direct, evidence, lifecycle, key, finished, name, profile, prompt, phase, latest, lastTool, provider, model, sources: [call, ...evidence] };
    });
    // Delivery/context records have no activation. Match them in completion
    // order, independently for each evidence channel.
    const completed = items.filter(item => item.finished).sort((a, b) =>
      (positions.get(a.finished!.anchor) ?? entries.length) - (positions.get(b.finished!.anchor) ?? entries.length));
    const folded = new Set<string>();
    for (const entry of entries) {
      if (entry.kind !== "tool" || entry.title !== "agent_status" || entry.remoteReview || entry.error ||
        ["Error", "Cancelled", "Approval denied", "Approval required", "Waiting for approval", "Interrupted", "Not run"].includes(entry.state || "") ||
        (!!entry.taskId && entry.taskId !== parentId)) continue;
      const args = argumentsOf(entry);
      if ("remote_operation" in args) continue;
      // An incomplete status request has no independent progress to display.
      // Wait for its target before deciding whether it belongs to a run or is
      // a standalone list/inspection. Errors remain ordinary visible records.
      if (entry.toolProgress === "streaming" && entry.result === undefined &&
        typeof args.session_id !== "string" && items.some(item => item.binding && item.direct && item.lifecycle)) {
        folded.add(entry.id); continue;
      }
      if (typeof args.session_id !== "string" || !args.session_id) continue;
      const owner = items.findLast(item => item.target === args.session_id && positions.get(item.call.id)! < positions.get(entry.id)!);
      if (!owner?.binding || !owner.direct || !owner.lifecycle) continue;
      if (entry.result !== undefined) {
        try {
          const result: unknown = JSON.parse(entry.result);
          if (!result || typeof result !== "object" || Array.isArray(result) ||
            !("session_id" in result) || result.session_id !== owner.target || "sessions" in result) continue;
          // On reload, an explicit status inspection may have delivered the
          // verified result before the later background notification. Preserve
          // that earlier parent-generation boundary, without trusting a poll
          // to establish the child's state or activation.
          const finished = owner.finished;
          if (finished && !finished.observed && !finished.generation && "status" in result &&
            result.status === (finished.phase === "done" ? "completed" : finished.phase) &&
            positions.get(entry.id)! < (positions.get(finished.anchor) ?? entries.length)) {
            finished.anchor = entry.id; finished.offset = .25;
          }
        } catch { continue; }
      }
      // Poll output is inspectable evidence, never lifecycle authority. In
      // particular it cannot complete, resume, or rename a child activation.
      owner.sources.push(entry); folded.add(entry.id);
    }
    const noticesUsed = new Set<string>(), contextsUsed = new Set<string>();
    for (const entry of entries) {
      const notice = entry.kind === "notification" && entry.cause === "completion" && entry.sourceTaskId &&
        (!(entry.taskId || "") || entry.taskId === parentId);
      const ids = notice ? [entry.sourceTaskId!] : savedTasks(entry);
      if (!ids.length) continue;
      const used = notice ? noticesUsed : contextsUsed;
      const updates = notice ? undefined : agentUpdates(entry.text);
      const chosen = new Set(used);
      const matches = ids.map((id, index) => {
        const activationMatches = (item: typeof items[number]) => !notice || entry.sourceActivation === undefined ||
          item.binding?.activation === entry.sourceActivation;
        const candidates = completed.filter(item => item.target === id && activationMatches(item) && !chosen.has(item.key) &&
          (positions.get(item.finished!.anchor) ?? 0) <= positions.get(entry.id)!);
        const update = updates?.sessions[index];
        const text = notice ? entry.text : update?.result ?? update?.error;
        // A checkpoint or notification can precede the terminal lifecycle.
        // The host notification's exact activation prevents a human/host resume
        // from borrowing an earlier native request. Neither source grants state.
        const canFoldEarly = notice ? entry.sourceActivation !== undefined
          : update && ["completed", "failed", "cancelled"].includes(update.status);
        const accepted = canFoldEarly
          ? items.findLast(item => item.binding && item.direct && item.target === id && activationMatches(item) &&
            !chosen.has(item.key) && positions.get(item.call.id)! < positions.get(entry.id)!) : undefined;
        const match = candidates.find(item => item.finished!.text === text || !!update?.truncated && !!text && item.finished!.text.startsWith(text)) || accepted || candidates.at(-1);
        if (match) chosen.add(match.key);
        return match;
      });
      if (matches.some(item => !item)) continue;
      for (const item of matches) { item!.sources.push(entry); used.add(item!.key); }
      if (!this.visibleEvidence.has(entry.id)) folded.add(entry.id);
    }
    const rows: PositionedEntry[] = [];
    for (const item of items) {
      const { call, binding, target, lifecycle, key, finished, sources } = item;
      const presentation: DelegationPresentation = { kind: "start", taskId: target, activation: binding?.activation,
        accepted: !!binding, detailsTruncated: finished?.detailsTruncated ?? lifecycle?.detailsTruncated, name: finished?.name || item.name, profile: finished?.profile || item.profile,
        phase: finished?.phase || item.phase, provider: finished?.provider ?? item.provider, model: finished?.model ?? item.model, prompt: finished?.prompt || item.prompt,
        text: call.error || finished?.text || "", latest: finished?.latest || item.latest, lastTool: finished?.lastTool || item.lastTool, sources };
      const definition = options.profiles?.find(profile => profile.id === presentation.profile);
      if (definition?.provider === "a2a") {
        presentation.profileName = profileLabel(definition);
        presentation.profileDescription = profileDescription(definition);
      }
      rows.push({ entry: { ...call, kind: "status", delegation: presentation, taskId: undefined,
        activity: Math.max(call.activity || 0, lifecycle?.activity || 0), title: presentation.name || presentation.profile }, position: positions.get(call.id)! });
      if (finished?.generationSettled) rows.push({ entry: { id: `delegation-result:${key}`, kind: "status", text: finished.text,
        title: finished.name || finished.profile, state: finished.phase === "failed" ? "Failed" : finished.phase,
        error: finished.phase === "failed" ? finished.text || "The subagent failed." : undefined,
        runId: call.runId, activity: lifecycle?.activity,
        delegation: { ...presentation, kind: "result", text: finished.text } },
        position: Math.max((positions.get(finished.anchor) ?? entries.length - 1) + finished.offset, positions.get(call.id)! + .25), order: finished.order });
    }
    const originals: PositionedEntry[] = entries.flatMap((entry, index) => {
      if (calls.includes(entry) || entry.kind === "retained_tasks" || entry.taskId || folded.has(entry.id)) return [];
      if (entry.kind === "context" || entry.kind === "notification") this.visibleEvidence.add(entry.id);
      return [{ entry, position: index }];
    });
    return [...originals, ...rows].sort((a, b) => a.position - b.position || (a.order || 0) - (b.order || 0)).map(item => item.entry);
  }
}

export function delegationPreview(item: DelegationPresentation): { tool: string; text: string; live: boolean } {
  if (item.kind === "result") return { tool: "", text: item.text, live: false };
  if (item.latest?.kind === "tool") {
    const raw = item.latest.inputs || "";
    let text = raw;
    try { text = JSON.stringify(JSON.parse(raw)); } catch { /* Partial arguments remain inspectable text. */ }
    return { tool: item.latest.title || "", text, live: item.latest.toolProgress === "streaming" };
  }
  return { tool: "", text: item.latest?.text || item.latest?.responseMetadata?.summary || item.text || item.prompt,
    live: item.phase === "running" || item.phase === "waiting" };
}
