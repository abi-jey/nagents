import { request } from "./client.js";
import { object, validSnapshot } from "./subscription.js";
import type { Session, Snapshot } from "../types.js";

export async function deleteSession(token: string, id: string): Promise<Snapshot> {
  const reply: unknown = await (await request(`sessions/${encodeURIComponent(id)}`, token, {}, undefined, "DELETE")).json();
  if (!object(reply) || reply.deleted_session_id !== id || !validSnapshot(reply) ||
      reply.session_id === id || reply.sessions.some((session) => session.id === id) ||
      !reply.sessions.some((session) => session.id === reply.session_id))
    throw new Error("Deletion acknowledgement was not confirmed. Your draft is kept. Reconnect to check the session list before retrying.");
  return reply;
}

export type DeletionState = { target?: Session; pending: boolean; error: string };
export class SessionDeletion {
  state: DeletionState = { pending: false, error: "" };
  constructor(private changed: (state: DeletionState) => void, private remove: (id: string) => Promise<boolean>) {}
  private update(state: DeletionState) { this.state = state; this.changed(state); }
  show(target: Session) {
    if (!this.state.pending) this.update({ target: { ...target }, pending: false, error: "" });
  }
  cancel() { if (!this.state.pending) this.update({ pending: false, error: "" }); }
  async confirm() {
    const target = this.state.target;
    if (!target || this.state.pending) return;
    this.update({ target, pending: true, error: "" });
    try {
      if (!await this.remove(target.id)) throw new Error("Finish the current operation before deleting.");
      this.update({ pending: false, error: "" });
    } catch (cause) {
      this.update({ target, pending: false, error: cause instanceof Error ? cause.message : "Deletion failed. Your draft is kept." });
    }
  }
}
