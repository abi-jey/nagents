import { RequestError } from "../../api/client.js";
import type { Session } from "../../types.js";
import { retentionDays, type RestoreReply, type TrashItem, type TrashSnapshot } from "../../api/trash.js";

export type TrashNotice = { title: string; status: "deleted" | "restored" | "purged"; item?: TrashItem; id: string };
export type TrashState = {
  open: boolean; loading: boolean; pending: string; error: string; draftDays: string;
  snapshot?: TrashSnapshot; notice?: TrashNotice;
};
export type TrashDependencies = {
  read: (signal: AbortSignal) => Promise<TrashSnapshot>;
  save: (revision: string, days: string) => Promise<TrashSnapshot>;
  softDelete: (id: string) => Promise<TrashItem>;
  permanent: (id: string) => Promise<void>;
  restore: (item: TrashItem) => Promise<RestoreReply>;
  purge: (item: TrashItem) => Promise<void>;
  restored: (reply: RestoreReply) => void;
  mutate: (action: () => Promise<void>) => Promise<void>;
  schedule?: (callback: () => void) => () => void;
};

export class TrashController {
  private state: TrashState = { open: false, loading: false, pending: "", error: "", draftDays: "30" };
  private listeners = new Set<() => void>();
  private reader?: AbortController;
  private cancelTimer?: () => void;
  private revision = "";
  private disposed = false;
  constructor(private deps: TrashDependencies) {}
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private update(update: Partial<TrashState>) {
    if (this.disposed) return;
    this.state = { ...this.state, ...update };
    this.listeners.forEach((listener) => listener());
  }
  private stopRead() { this.reader?.abort(); this.reader = undefined; this.cancelTimer?.(); this.cancelTimer = undefined; }
  private poll() {
    this.cancelTimer?.();
    if (!this.state.open || this.disposed) return;
    this.cancelTimer = (this.deps.schedule || ((callback) => {
      const timer = setTimeout(callback, 30000); return () => clearTimeout(timer);
    }))(() => { this.cancelTimer = undefined; void this.refresh(); });
  }
  show = () => { this.disposed = false; this.update({ open: true, error: "" }); void this.refresh(); };
  close = () => {
    if (this.state.pending) return;
    this.stopRead(); this.revision = "";
    this.update({ open: false, loading: false, draftDays: String(this.state.snapshot?.retention_days ?? 30) });
  };
  dispose = () => { this.stopRead(); this.disposed = true; };
  activate = () => { this.disposed = false; };
  async refresh(force = false): Promise<void> {
    if (this.disposed || this.state.pending || (!this.state.open && !force)) return;
    this.stopRead();
    const reader = new AbortController(); this.reader = reader;
    this.update({ loading: true });
    try {
      const snapshot = await this.deps.read(reader.signal);
      if (reader.signal.aborted || this.reader !== reader || this.disposed) return;
      this.update({ snapshot, ...(!this.revision ? { draftDays: String(snapshot.retention_days) } : {}) });
    } catch (cause) {
      if (!reader.signal.aborted && this.reader === reader)
        this.update({ error: cause instanceof Error ? cause.message : "Trash could not be refreshed." });
    } finally {
      if (this.reader === reader) { this.reader = undefined; this.update({ loading: false }); this.poll(); }
    }
  }
  editDays = (days: string) => {
    if (this.state.pending) return;
    if (!this.revision) this.revision = this.state.snapshot?.revision || "";
    this.update({ draftDays: days });
  };
  cancelEdit = () => {
    if (this.state.pending) return;
    this.revision = ""; this.update({ draftDays: String(this.state.snapshot?.retention_days ?? 30), error: "" });
  };
  private async operation(name: string, action: () => Promise<void>): Promise<void> {
    if (this.state.pending || this.disposed) throw new Error("Finish the current Trash operation first.");
    this.stopRead(); this.update({ pending: name, loading: false, error: "" });
    let stale = false;
    try { await this.deps.mutate(action); }
    catch (cause) {
      stale = cause instanceof RequestError && [409, 410].includes(cause.status);
      this.update({ error: (cause instanceof Error ? cause.message : "Trash operation failed.") +
        " No operation was retried." + (stale ? " Refresh Trash before retrying." : "") });
      throw cause;
    } finally {
      this.update({ pending: "" });
      await this.refresh(stale);
      if (stale && this.revision) this.revision = this.state.snapshot?.revision || this.revision;
    }
  }
  move = async (session: Session): Promise<void> => {
    await this.operation(session.id, async () => {
      const item = await this.deps.softDelete(session.id);
      this.update({ notice: { title: session.title, id: item.id, item, status: "deleted" } });
    });
  };
  restore = async (item: TrashItem): Promise<void> => {
    const captured = { ...item };
    await this.operation(item.id, async () => {
      const reply = await this.deps.restore(captured);
      this.deps.restored(reply); // Membership only. No resume, draft or subscription mutation.
      this.update({ notice: { title: captured.title, id: captured.id, status: "restored" } });
    });
  };
  permanent = async (id: string, item?: TrashItem): Promise<void> => {
    const captured = item && { ...item };
    await this.operation(id, async () => {
      if (captured) await this.deps.purge(captured);
      else await this.deps.permanent(id);
      this.update({ notice: { title: captured?.title || "Session", id, status: "purged" } });
    });
  };
  save = async (): Promise<void> => {
    try { retentionDays(this.state.draftDays); }
    catch (cause) { this.update({ error: (cause as Error).message }); return; }
    const revision = this.revision || this.state.snapshot?.revision || "";
    const days = this.state.draftDays;
    await this.operation("settings", async () => {
      const snapshot = await this.deps.save(revision, days);
      this.revision = ""; this.update({ snapshot, draftDays: String(snapshot.retention_days) });
    });
  };
  dismiss = () => { if (!this.state.pending) this.update({ error: "", notice: undefined }); };
}
