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

export type DeletionSelection = { id: string; revision: number };
export type DeletionView = {
  selection: () => DeletionSelection;
  remove: (id: string) => Promise<Snapshot>;
  drop: (id: string) => void;
  forget: (id: string, discardDraft: boolean) => void;
  replace: (snapshot: Snapshot) => void;
  pause: () => void;
  reconnect: () => void;
};

export async function deleteSessionFromView(id: string, view: DeletionView): Promise<void> {
  const selected = view.selection();
  const deletingCurrent = id === selected.id;
  const unchanged = () => {
    const latest = view.selection();
    return latest.id === selected.id && latest.revision === selected.revision;
  };
  let replaced = false;
  if (deletingCurrent) view.pause();
  try {
    const snapshot = await view.remove(id);
    // The server's selected root is shared across tabs. Its snapshot is a
    // replacement only for the exact browser selection we started deleting.
    const replaceCurrent = deletingCurrent && unchanged();
    view.drop(id);
    view.forget(id, replaceCurrent);
    if (replaceCurrent) {
      view.replace(snapshot);
      replaced = true;
    }
  } finally {
    // An unrelated deletion never touches the live subscription. A newer
    // selection owns its own transport, even after an away-and-back navigation.
    if (deletingCurrent && (replaced || unchanged())) view.reconnect();
  }
}

export function restoreDeletionFocus(previous: HTMLElement | null, fallback: HTMLElement | null, mobileNavigation: HTMLElement | null = null): void {
  const candidates = [
    previous,
    mobileNavigation?.querySelector<HTMLElement>(".session-row[data-session-id] > .session[aria-current='page']:not(:disabled)"),
    mobileNavigation?.querySelector<HTMLElement>(".sidebar-close"),
    fallback,
  ];
  for (const target of candidates) {
    if (!target?.isConnected || target.matches(":disabled, [aria-disabled='true']") ||
        target.closest("[inert], [hidden], [aria-hidden='true']") || !target.getClientRects().length) continue;
    const style = target.ownerDocument.defaultView?.getComputedStyle(target);
    if (style?.visibility === "hidden" || style?.visibility === "collapse" || style?.opacity === "0") continue;
    target.focus({ preventScroll: true });
    // A remaining native modal can also make a candidate implicitly inert.
    if (target.ownerDocument.activeElement === target) return;
  }
}

export type DeletionState = { target?: Session; currentSessionId?: string; pending: boolean; error: string };
export class SessionDeletion {
  state: DeletionState = { pending: false, error: "" };
  constructor(private changed: (state: DeletionState) => void, private remove: (id: string) => Promise<boolean>,
    private currentSessionId?: () => string) {}
  private update(state: DeletionState) { this.state = state; this.changed(state); }
  show(target: Session) {
    if (!this.state.pending) this.update({ target: { ...target }, currentSessionId: this.currentSessionId?.() ?? target.id, pending: false, error: "" });
  }
  cancel() { if (!this.state.pending) this.update({ pending: false, error: "" }); }
  async confirm() {
    const target = this.state.target;
    const currentSessionId = this.state.currentSessionId;
    if (!target || this.state.pending) return;
    this.update({ target, currentSessionId, pending: true, error: "" });
    try {
      if (!await this.remove(target.id)) throw new Error("Finish the current operation before deleting.");
      this.update({ pending: false, error: "" });
    } catch (cause) {
      this.update({ target, currentSessionId, pending: false, error: cause instanceof Error ? cause.message : "Deletion failed. Your draft is kept." });
    }
  }
}
