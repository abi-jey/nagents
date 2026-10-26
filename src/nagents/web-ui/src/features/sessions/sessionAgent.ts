import { request, RequestError } from "../../api/client.js";

export type SessionAgentIdentity = {
  agent: string;
  display_name?: string;
  model: string;
  reasoning_effort?: string;
  provider: string;
  provider_kind: string;
  mode: "build" | "reviewer";
};
export type SessionAgentProfile = Omit<SessionAgentIdentity, "agent"> & {
  name: string;
  description: string;
  disabled_reason: string;
};
export type SessionAgentSelection = {
  session_id: string;
  revision: string;
  agent: string;
  workspace_default: string;
  effective: SessionAgentIdentity;
  active: SessionAgentIdentity | null;
  pending: boolean;
  profiles: SessionAgentProfile[];
};

function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function identity(value: unknown): value is SessionAgentIdentity {
  return object(value) && ["agent", "model", "provider", "provider_kind"].every(key => typeof value[key] === "string") &&
    (value.mode === "build" || value.mode === "reviewer");
}
export function validSessionAgent(value: unknown): value is SessionAgentSelection {
  return object(value) && ["session_id", "revision", "agent", "workspace_default"].every(key => typeof value[key] === "string") &&
    !!value.session_id && !!value.revision && !!value.agent && identity(value.effective) &&
    (value.active === null || identity(value.active)) && typeof value.pending === "boolean" &&
    Array.isArray(value.profiles) && value.profiles.every(profile => object(profile) &&
      ["name", "description", "model", "provider", "provider_kind", "disabled_reason"].every(key => typeof profile[key] === "string") &&
      (profile.mode === "build" || profile.mode === "reviewer"));
}

async function selection(response: Response, sessionId: string): Promise<SessionAgentSelection> {
  const value: unknown = await response.json();
  if (!validSessionAgent(value) || value.session_id !== sessionId) throw new Error("Conversation agent could not be read. Refresh to try again.");
  return value;
}
export function readSessionAgent(token: string, sessionId: string, signal: AbortSignal) {
  return request(`sessions/${encodeURIComponent(sessionId)}/agent`, token, undefined, signal).then(response => selection(response, sessionId));
}
export function saveSessionAgent(token: string, sessionId: string, revision: string, agent: string, signal: AbortSignal) {
  return request(`sessions/${encodeURIComponent(sessionId)}/agent`, token, { revision, agent }, signal).then(response => selection(response, sessionId));
}

/** Own reads and saves per session. Incoming snapshots invalidate older HTTP reads. */
export class SessionAgentController {
  private disposed = false;
  private epoch = 0;
  private reader?: AbortController;
  private writer?: AbortController;
  private current?: SessionAgentSelection;
  constructor(private readonly options: {
    sessionId: string;
    read: (signal: AbortSignal) => Promise<SessionAgentSelection>;
    save: (revision: string, agent: string, signal: AbortSignal) => Promise<SessionAgentSelection>;
    accept: (selection: SessionAgentSelection) => void;
    error: (message: string) => void;
    loading: (value: boolean) => void;
    saving: (value: boolean) => void;
  }) {}

  accept(value: unknown) {
    if (this.disposed || !validSessionAgent(value) || value.session_id !== this.options.sessionId) return;
    this.epoch++;
    this.reader?.abort(); this.reader = undefined;
    this.options.loading(false);
    this.current = value;
    this.options.accept(value);
  }

  async refresh(preserveError = false) {
    if (this.disposed) return;
    this.reader?.abort();
    const reader = this.reader = new AbortController(), epoch = ++this.epoch;
    this.options.loading(true);
    try {
      const value = await this.options.read(reader.signal);
      if (!this.disposed && !reader.signal.aborted && epoch === this.epoch) {
        this.accept(value);
        if (!preserveError) this.options.error("");
      }
    } catch (cause) {
      if (!this.disposed && !reader.signal.aborted && epoch === this.epoch)
        this.options.error(cause instanceof Error ? cause.message : "Conversation agent could not be read.");
    } finally {
      if (!this.disposed && this.reader === reader) { this.reader = undefined; this.options.loading(false); }
    }
  }

  async choose(agent: string): Promise<boolean> {
    if (this.disposed || this.writer) return false;
    if (!this.current) await this.refresh();
    const current = this.current;
    if (this.disposed || this.writer || !current) return false;
    const profile = current.profiles.find(profile => profile.name === agent);
    const reason = profile?.disabled_reason || (!profile ? `Agent profile not found: ${agent}.` : "");
    if (reason) { this.options.error(reason); return false; }
    const writer = this.writer = new AbortController(), epoch = this.epoch;
    this.options.saving(true); this.options.error("");
    try {
      const value = await this.options.save(current.revision, agent, writer.signal);
      if (this.disposed || writer.signal.aborted) return false;
      // A WebSocket update may already contain a newer selection or active run.
      if (epoch === this.epoch) this.accept(value);
      else void this.refresh();
      return true;
    } catch (cause) {
      if (!this.disposed && !writer.signal.aborted) {
        this.options.error(cause instanceof Error ? cause.message : "Conversation agent could not be saved.");
        if (cause instanceof RequestError && cause.status === 409) await this.refresh(true);
      }
      return false;
    } finally {
      this.writer = undefined;
      if (!this.disposed) this.options.saving(false);
    }
  }

  dispose() { this.disposed = true; this.epoch++; this.reader?.abort(); this.writer?.abort(); }
}

export function agentCommand(argument: string): { scope: "conversation" | "workspace"; name: string } {
  const value = argument.trim();
  const workspace = value === "--workspace" || value.startsWith("--workspace ");
  const name = workspace ? value.slice("--workspace".length).trim() : value;
  if (name.startsWith("-") || name.includes(" ")) throw new Error("Usage: /agent [name] or /agent --workspace [name]. Your draft is kept.");
  return { scope: workspace ? "workspace" : "conversation", name };
}
