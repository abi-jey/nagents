import type { Entry } from "../features/chat/transcript.js";
import { isArchive, NativeTranscript } from "./transcript.js";
import {
  browserTransport, runPath, sessionPath,
  type Approval, type Bootstrap, type Event, type Queue, type QueueAction, type Run, type RunStatus,
  type Session, type Snapshot, type SteeringReceipt, type SteeringState, type Task, type TaskLimits, type Transport,
} from "./transport.js";

type Draft = { text: string; revision: number };
type Binding = {
  id: string; session: string; prompt: string; run?: string;
  state: "sending" | "unconfirmed" | "confirmed" | "rejected";
};
type View = Snapshot & {
  config?: Bootstrap;
  session: string; sessions: Session[]; entries: Entry[]; connected: boolean; connecting: boolean; reconnecting: boolean;
  creating: boolean; draft: string; bindings: readonly Binding[];
  tasks: Task[]; limits?: TaskLimits; queue?: Queue; queueLimit: number; steering: readonly SteeringState[];
  child?: { task: Task; entries: Entry[] };
  deciding?: string; stopping?: string; queueBusy: boolean; error: string; warning: string;
};
type Refresh = "sessions" | "state" | "queue" | "tasks" | `steering:${string}`;
const emptyDraft: Draft = { text: "", revision: 0 };
const detail = (error: unknown) => error instanceof Error ? error.message : "Unexpected ngn response";

export function activeRun(view: View): Run | undefined { return view.runs.find(run => run.session_id === view.session); }
export function pendingApproval(view: View): Approval | undefined {
  return view.approvals.find(approval => approval.root_session_id === view.session || approval.session_id === view.session);
}
export function steeringTarget(view: View): string | undefined {
  const run = activeRun(view);
  if (!view.connected || !run || view.child) return;
  if (view.steering.some(state => state.run_id === run.id && state.session_id === view.session &&
    state.run_status === "running" && state.supported && state.accepting)) return run.id;
}

/** One Rust transport for both the root conversation and the read-only child inspector. */
export class NativeClient {
  private view: View = {
    session: "", sessions: [], entries: [], connected: false, connecting: false, reconnecting: false,
    creating: false, draft: "", bindings: [],
    runs: [], approvals: [], tasks: [], queueLimit: 32, queueBusy: false, steering: [], error: "", warning: "",
  };
  private listeners = new Set<() => void>();
  private boot = new AbortController();
  private scope = new AbortController();
  private closeRoot?: () => void;
  private closeChild?: () => void;
  private flush?: ReturnType<typeof setTimeout>;
  private transcript?: NativeTranscript;
  private transcriptDirty = false;
  private revisions = new Map<Refresh, number>();
  private pending = new Set<Refresh>();
  private seenSteering = new Set<string>();
  private drafts = new Map<string, Draft>();

  constructor(private transport: Transport = browserTransport, private requestId: () => string = () => crypto.randomUUID()) {}

  snapshot = () => this.view;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };

  private update(patch: Partial<View>) {
    this.view = { ...this.view, ...patch };
    this.listeners.forEach(listener => listener());
  }

  private closeSession() {
    this.scope.abort();
    this.scope = new AbortController();
    this.closeRoot?.();
    this.closeChild?.();
    this.closeRoot = this.closeChild = undefined;
    clearTimeout(this.flush);
    this.flush = undefined;
    this.pending.clear();
    this.revisions.clear();
    this.seenSteering.clear();
    this.transcriptDirty = false;
  }

  dispose() {
    this.boot.abort();
    this.closeSession();
  }

  async connect() {
    this.dispose();
    this.boot = new AbortController();
    const { signal } = this.boot;
    this.update({ connected: false, connecting: true, reconnecting: false, creating: false, approvals: [], error: "" });
    try {
      const config = await this.transport.request<Bootstrap>("/api/bootstrap", undefined, undefined, signal);
      if (config.protocol_version !== 1 || !config.token) throw new Error("This browser needs ngn web protocol v1.");
      const { sessions } = await this.transport.request<{ sessions: Session[] }>("/api/sessions", config.token, undefined, signal);
      if (signal.aborted) return;
      this.update({ config, sessions, connecting: false });
      const previous = sessions.find(session => session.id === this.view.session);
      const session = previous || sessions[0];
      if (session) this.select(session.id);
      else await this.create();
    } catch (error) {
      if (!signal.aborted) this.update({ connecting: false, error: detail(error) });
    }
  }

  async create(): Promise<boolean> {
    const { config, creating, connecting } = this.view;
    if (!config || creating || connecting) return false;
    const { signal } = this.boot;
    this.update({ creating: true, error: "" });
    try {
      const { session } = await this.transport.request<{ session: Session }>("/api/sessions", config.token, {}, signal);
      if (signal.aborted) return false;
      this.update({ sessions: [session, ...this.view.sessions.filter(item => item.id !== session.id)] });
      this.select(session.id);
      return true;
    } catch (error) {
      if (!signal.aborted) this.update({ error: detail(error) });
      return false;
    } finally {
      if (!signal.aborted) this.update({ creating: false });
    }
  }

  select(session: string) {
    if (!this.view.config || !this.view.sessions.some(item => item.id === session)) return;
    if (session === this.view.session && this.closeRoot) return;
    this.closeSession();
    const { signal } = this.scope;
    this.transcript = new NativeTranscript(session);
    this.update({ session, entries: [], tasks: [], limits: undefined, queue: undefined, steering: [], child: undefined,
      connected: false, reconnecting: false, approvals: [], error: "", warning: "", draft: this.getDraft(session).text,
      queueBusy: false, deciding: undefined, stopping: undefined });
    const refresh = () => this.invalidate("state", "sessions", ...(isArchive(session) ? [] : ["tasks", "queue"] as const));
    this.closeRoot = this.transport.watch(session, this.view.config.token, record => {
      if (signal.aborted || record.session_id !== session) return;
      if (this.transcript?.apply(record)) { this.transcriptDirty = true; this.schedule(); }
      this.onEvent(record);
    }, connected => {
      if (signal.aborted) return;
      // A previous connection's snapshot may be stale, including a decision
      // another client made while this socket was down.
      if (connected) refresh();
      this.update({ connected, reconnecting: !connected, approvals: [] });
      if (connected) {
        for (const run of this.seenSteering) this.invalidate(`steering:${run}`);
      }
    }, () => { if (!signal.aborted) this.update({ error: "Invalid conversation event; reconnect to ngn." }); });
    refresh();
    for (const binding of this.view.bindings) if (binding.session === session && binding.run) {
      this.seenSteering.add(binding.run);
      this.invalidate(`steering:${binding.run}`);
    }
  }

  inspect(id: string) {
    const { session, tasks, config, child } = this.view;
    if (child?.task.session_id === id) return;
    this.closeChild?.();
    this.closeChild = undefined;
    if (!id) { this.update({ child: undefined }); return; }
    const task = tasks.find(task => task.session_id === id && task.root_session_id === session);
    if (!task || !config) { this.update({ child: undefined }); return; }
    const { signal } = this.scope;
    const transcript = new NativeTranscript(id, task.run_id);
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined;
    this.update({ child: { task, entries: [] } });
    const unwatch = this.transport.watch(id, config.token, record => {
      if (!stopped && !signal.aborted && transcript.apply(record)) {
        timer ??= setTimeout(() => {
          timer = undefined;
          const current = this.view.child;
          if (!stopped && current?.task.run_id === task.run_id)
            this.update({ child: { ...current, entries: transcript.snapshot() } });
        }, 32);
      }
    }, () => {}, () => { if (!stopped) this.update({ error: "Invalid agent conversation event." }); });
    this.closeChild = () => { stopped = true; clearTimeout(timer); unwatch(); };
  }

  private onEvent(record: Event) {
    switch (record.event) {
      case "run_started":
        this.invalidate("state", "sessions", "queue");
        break;
      case "run_finished":
        this.invalidate("state", "sessions", "queue");
        if (this.seenSteering.has(record.run_id)) this.invalidate(`steering:${record.run_id}`);
        break;
      case "run_queued":
      case "queue_updated":
        this.invalidate("queue");
        break;
      case "approval_requested":
      case "approval_resolved":
        this.invalidate("state");
        break;
      case "task_started":
      case "task_updated":
      case "task_finished":
        this.invalidate("tasks", "state");
        break;
      case "steering_updated":
        if (record.input_id || record.run_id === activeRun(this.view)?.id || this.seenSteering.has(record.run_id)) {
          if (record.input_id) this.seenSteering.add(record.run_id);
          this.invalidate(`steering:${record.run_id}`);
        }
        break;
    }
  }

  private invalidate(...keys: Refresh[]) {
    for (const key of keys) {
      this.revisions.set(key, (this.revisions.get(key) || 0) + 1);
      this.pending.add(key);
    }
    this.schedule();
  }

  private schedule() {
    this.flush ??= setTimeout(() => {
      this.flush = undefined;
      if (this.transcriptDirty && this.transcript) {
        this.transcriptDirty = false;
        this.update({ entries: this.transcript.snapshot() });
      }
      const keys = [...this.pending];
      this.pending.clear();
      keys.forEach(key => void this.refresh(key));
    }, 32);
  }

  private async refresh(key: Refresh) {
    const { signal } = this.scope;
    const { session, config } = this.view;
    if (!config || !session) return;
    const revision = this.revisions.get(key);
    const latest = () => !signal.aborted && revision === this.revisions.get(key);
    const get = <T>(path: string) => this.transport.request<T>(path, config.token, undefined, signal);
    try {
      if (key === "sessions") {
        const { sessions } = await get<{ sessions: Session[] }>("/api/sessions");
        if (latest()) this.update({ sessions });
      } else if (key === "state") {
        if (!this.view.connected) return;
        const previous = activeRun(this.view);
        const state = await get<Snapshot>("/api/state");
        if (!latest() || !this.view.connected) return;
        this.update(state);
        const current = activeRun(this.view);
        if (current) this.invalidate(`steering:${current.id}`);
        if (previous && previous.id !== current?.id && this.seenSteering.has(previous.id))
          this.invalidate(`steering:${previous.id}`);
      } else if (key === "queue") {
        if (isArchive(session)) return;
        const { queue, limit } = await get<{ queue: Queue; limit: number }>(`${sessionPath(session)}/queue`);
        if (latest() && queue.session_id === session) this.update({ queue, queueLimit: limit });
      } else if (key === "tasks") {
        if (isArchive(session)) return;
        const { tasks, limits } = await get<{ tasks: Task[]; limits: TaskLimits }>(`${sessionPath(session)}/tasks`);
        if (!latest()) return;
        const scoped = tasks.filter(task => task.root_session_id === session);
        const { child } = this.view;
        const updated = scoped.find(task => task.run_id === child?.task.run_id);
        this.update({ tasks: scoped, limits, ...(child && updated && { child: { ...child, task: updated } }) });
      } else {
        const run = key.slice("steering:".length);
        const { steering } = await get<{ steering: SteeringState }>(`${runPath(run)}/steer`);
        if (latest() && steering.run_id === run && steering.session_id === session) this.applySteering(steering);
      }
    } catch (error) {
      if (latest()) this.update({ error: detail(error) });
    }
  }

  private applySteering(state: SteeringState) {
    const receipts = state.receipts.filter(receipt => receipt.run_id === state.run_id && receipt.session_id === state.session_id);
    const bindings = this.view.bindings.map(binding => {
      if (binding.run !== state.run_id || binding.session !== state.session_id || binding.state !== "unconfirmed") return binding;
      if (receipts.some(receipt => receipt.request_id === binding.id && receipt.prompt === binding.prompt))
        return { ...binding, state: "confirmed" as const };
      // A conflict can occur after persistence. Only a terminal exact-run
      // snapshot with no receipt makes it safe to explicitly release a draft.
      if (!state.accepting && ["completed", "failed", "cancelled", "interrupted"].includes(state.run_status))
        return { ...binding, state: "rejected" as const };
      return binding;
    });
    if (receipts.length) this.seenSteering.add(state.run_id);
    this.update({ steering: [...this.view.steering.filter(item => item.run_id !== state.run_id), { ...state, receipts }], bindings });
  }

  private getDraft(session: string) { return this.drafts.get(session) || emptyDraft; }

  setDraft(text: string) {
    const { session } = this.view;
    this.drafts.set(session, { text, revision: this.getDraft(session).revision + 1 });
    this.update({ draft: text });
  }

  dismiss() { this.update({ error: "" }); }

  async send(prompt = this.view.draft, steer = false) {
    const { session, config, connected, bindings, child } = this.view;
    if (!config || !session || !connected || child || isArchive(session) || !prompt.trim() ||
        bindings.some(binding => binding.session === session && binding.state === "sending")) return;
    if (new TextEncoder().encode(prompt).length > 32_000) {
      this.update({ error: "Messages must be at most 32,000 UTF-8 bytes." });
      return;
    }
    const bound = bindings.find(binding => binding.prompt === prompt);
    if (bound && (bound.session !== session || bound.run || steer)) {
      this.update({ error: bound.run ? "This draft is bound to a steering run. Review its receipt; it cannot be queued." :
        "This draft is bound to a queued submission. Retry Send in its original conversation." });
      return;
    }
    const run = steer ? steeringTarget(this.view) : undefined;
    if (steer && !run) return;
    if (!bound && bindings.length >= 16) {
      this.update({ error: "Review unconfirmed submissions before sending more." });
      return;
    }
    await this.postInput(bound || { id: this.requestId(), session, prompt, run, state: "sending" });
  }

  async retry(id: string) {
    const binding = this.view.bindings.find(item => item.id === id && item.session === this.view.session);
    if (!binding || binding.state !== "unconfirmed" || !this.view.connected ||
        this.view.bindings.some(item => item.session === binding.session && item.state === "sending")) return;
    await this.postInput(binding);
  }

  release(id: string) {
    const binding = this.view.bindings.find(item => item.id === id && item.session === this.view.session);
    if (!binding || (binding.state !== "rejected" && binding.state !== "confirmed")) return;
    if (binding.state === "confirmed" && this.view.draft === binding.prompt) this.setDraft("");
    this.update({ bindings: this.view.bindings.filter(item => item.id !== id), error: "" });
  }

  private async postInput(binding: Binding) {
    const config = this.view.config;
    if (!config) return;
    const { session, prompt, run, id } = binding;
    const { signal } = this.boot;
    const draft = this.getDraft(session);
    const set = (next?: Binding["state"]) => this.update({ bindings: [
      ...this.view.bindings.filter(item => item.id !== id), ...(next ? [{ ...binding, state: next }] : []),
    ] });
    const input = { prompt, request_id: id };
    set("sending");
    this.update({ error: "", warning: "" });
    try {
      let warning = "";
      if (run) {
        const { receipt } = await this.transport.request<{ receipt: SteeringReceipt }>(`${runPath(run)}/steer`, config.token, input);
        if (receipt.run_id !== run || receipt.session_id !== session || receipt.request_id !== id || receipt.prompt !== prompt)
          throw new Error("Steering response identity mismatch");
      } else {
        const reply = await this.transport.request<{ run: Omit<Run, "status"> & { status: RunStatus | null }; warning?: string }>(
          `${sessionPath(session)}/runs`, config.token, input);
        if (reply.run.session_id !== session || reply.run.request_id !== id) throw new Error("Submission response identity mismatch");
        warning = reply.warning ? `Input accepted — ${reply.warning}` : "";
      }
      set();
      if (draft === this.getDraft(session) && draft.text === prompt) {
        this.drafts.set(session, { text: "", revision: draft.revision + 1 });
        if (this.view.session === session) this.update({ draft: "" });
      }
      if (this.view.session === session && !signal.aborted) this.update({ warning });
    } catch (error) {
      set("unconfirmed");
      if (this.view.session === session && !signal.aborted)
        this.update({ error: `${detail(error)} — draft retained. Retry only the original submission.` });
    } finally {
      if (this.view.session === session && !signal.aborted) {
        this.invalidate("state", "queue", "sessions");
        if (run) {
          this.seenSteering.add(run);
          this.invalidate(`steering:${run}`);
        }
      }
    }
  }

  async controlQueue(action: QueueAction) {
    const { session, queue, connected, queueBusy } = this.view;
    if (!queue || queue.session_id !== session || !connected || queueBusy) return;
    const { signal } = this.scope;
    this.update({ queueBusy: true });
    await this.mutate(`${sessionPath(session)}/queue`, { action }, "queue", "state");
    if (!signal.aborted) this.update({ queueBusy: false });
  }

  async cancel(run: string) {
    const { child, stopping, queue, connected } = this.view;
    if (!connected || stopping || !(run === activeRun(this.view)?.id || run === pendingApproval(this.view)?.run_id ||
        run === child?.task.run_id || queue?.entries.some(entry => entry.run.id === run))) return;
    const { signal } = this.scope;
    this.update({ stopping: run });
    await this.mutate(`${runPath(run)}/cancel`, {}, "state", "queue", "tasks");
    if (!signal.aborted) this.update({ stopping: undefined });
  }

  async decide(approval: Approval, allow: boolean) {
    const { signal } = this.scope;
    const pending = pendingApproval(this.view);
    if (!pending || approval.id !== pending.id || approval.run_id !== pending.run_id || this.view.deciding || !this.view.connected) return;
    this.update({ deciding: approval.id });
    const accepted = await this.mutate("/api/approvals", { run_id: approval.run_id, approval_id: approval.id, allow }, "state");
    if (!signal.aborted) {
      this.update({ deciding: undefined,
        ...(accepted && { approvals: this.view.approvals.filter(item => item.id !== approval.id) }) });
    }
  }

  private async mutate(path: string, body: object, ...refresh: Refresh[]) {
    const { signal } = this.scope;
    const token = this.view.config?.token;
    if (!token) return false;
    this.update({ error: "" });
    try {
      await this.transport.request(path, token, body);
      return true;
    } catch (error) {
      if (!signal.aborted) this.update({ error: detail(error) });
      return false;
    } finally {
      if (!signal.aborted) this.invalidate(...refresh);
    }
  }
}
