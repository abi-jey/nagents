import { request, requestBinary } from "../../api/client.js";

export type Upload = { upload_id: string; session_id: string; filename: string; media_type: string; byte_length: number; expires_at: number };
export type DraftUpload = { id: string; name: string; type: string; url: string; status: "uploading" | "ready" | "error"; error: string; upload?: Upload };
export type UploadState = { items: DraftUpload[]; types: string[]; error: string };
export type UploadMetadata = Pick<Upload, "upload_id" | "filename" | "media_type" | "byte_length">;
const TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"]);
export function uploadedAttachments(value: unknown): value is UploadMetadata[] {
  return Array.isArray(value) && value.length <= 3 && value.every((item: unknown) => {
    if (!item || typeof item !== "object") return false;
    const asset = item as Record<string, unknown>;
    return typeof asset.upload_id === "string" && typeof asset.filename === "string" &&
      typeof asset.media_type === "string" && TYPES.has(asset.media_type) &&
      typeof asset.byte_length === "number" && Number.isSafeInteger(asset.byte_length) && asset.byte_length > 0 && asset.byte_length <= 8 * 1024 * 1024;
  });
}

export class UploadDraft {
  private listeners = new Set<() => void>();
  private state: UploadState = { items: [], types: [], error: "" };
  private requests = new Map<string, AbortController>();
  private root = "";
  private token = "";
  private capabilityRequest?: AbortController;
  private submitting = false;
  private generation = 0;
  private uncertain = new Set<string>();
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.state;
  private publish(update: Partial<UploadState>) { this.state = { ...this.state, ...update }; for (const listener of this.listeners) listener(); }
  configure(token: string, root: string) {
    if (root !== this.root) { this.clear(); this.publish({ types: [], error: "" }); }
    this.root = root; this.token = token;
    this.capabilityRequest?.abort();
    if (!root || !token) return;
    const controller = this.capabilityRequest = new AbortController();
    void request(`sessions/${encodeURIComponent(root)}/uploads`, token, undefined, controller.signal)
      .then((response) => response.json()).then((body: unknown) => {
        if (controller.signal.aborted || this.capabilityRequest !== controller) return;
        if (!body || typeof body !== "object" || !("file_media_types" in body) || !Array.isArray(body.file_media_types)) throw new Error("Invalid upload capabilities.");
        this.publish({ types: body.file_media_types.filter((type): type is string => typeof type === "string" && TYPES.has(type)) });
      }).catch(() => { if (!controller.signal.aborted) this.publish({ types: [], error: "Attachment capabilities unavailable. Text submission is still available." }); });
  }
  async add(files: File[]) {
    if (this.submitting) return;
    const generation = this.generation, root = this.root;
    for (const file of files) {
      if (this.generation !== generation || this.submitting) break;
      if (this.state.items.length >= 3 || file.size <= 0 || file.size > 8 * 1024 * 1024 || !this.state.types.includes(file.type)) {
        this.publish({ error: "Choose up to three supported images/PDFs, at most 8 MiB each. The current provider may support fewer formats." }); continue;
      }
      const token = this.token, id = crypto.randomUUID();
      const controller = new AbortController();
      this.requests.set(id, controller);
      const item: DraftUpload = { id, name: file.name, type: file.type, status: "uploading", error: "", url: file.type.startsWith("image/") ? URL.createObjectURL(file) : "" };
      this.publish({ items: [...this.state.items, item], error: "" });
      // Each batch uploads sequentially, retaining at most three draft files.
      try {
        const response = await requestBinary(`sessions/${encodeURIComponent(root)}/uploads/${id}`, token, file,
          { "X-Ngn-Filename": encodeURIComponent(file.name) }, controller.signal);
        const upload = await response.json() as Upload;
        if (!upload || upload.upload_id !== id || upload.session_id !== root || upload.byte_length !== file.size || upload.media_type !== file.type ||
            !Number.isFinite(upload.expires_at))
          throw new Error("Upload acknowledgement did not match the draft.");
        if (this.generation !== generation || !this.state.items.some((entry) => entry.id === id)) { this.discard(root, token, id); continue; }
        this.publish({ items: this.state.items.map((entry) => entry.id === id ? { ...entry, status: "ready", upload } : entry) });
      } catch (error) {
        if (this.generation === generation && this.state.items.some((entry) => entry.id === id))
          this.publish({ items: this.state.items.map((entry) => entry.id === id ? { ...entry, status: "error", error: error instanceof Error ? error.message : "Upload failed. Remove and attach again." } : entry) });
      } finally { this.requests.delete(id); }
    }
  }
  private discard(root: string, token: string, id: string) {
    void request(`sessions/${encodeURIComponent(root)}/uploads/${id}`, token, {}, undefined, "DELETE").catch(() => {});
    // A disconnected browser cannot guarantee deletion; server TTL is authoritative.
  }
  remove(id: string) {
    if (this.submitting) return;
    const item = this.state.items.find((entry) => entry.id === id);
    if (!item) return;
    this.requests.get(id)?.abort(); this.requests.delete(id);
    if (item.url) URL.revokeObjectURL(item.url);
    this.discard(this.root, this.token, id);
    this.uncertain.delete(id);
    this.publish({ items: this.state.items.filter((entry) => entry.id !== id) });
  }
  clear() {
    this.generation++;
    const locked = this.submitting;
    this.submitting = false;
    for (const item of this.state.items) this.remove(item.id);
    this.submitting = locked;
  }
  begin() {
    if (this.submitting || this.state.items.some((item) => item.status !== "ready")) throw new Error("Finish or remove pending/failed attachments before sending.");
    if (this.state.items.some((item) => !this.uncertain.has(item.id) && (item.upload?.expires_at || 0) * 1000 <= Date.now())) throw new Error("An attachment expired. Remove and attach it again.");
    this.submitting = true;
    return this.state.items.map((item) => item.id);
  }
  finish(ids: string[], confirmed: boolean) {
    this.submitting = false;
    if (!confirmed) for (const id of ids) this.uncertain.add(id);
    if (confirmed) for (const id of ids) {
      this.uncertain.delete(id);
      const item = this.state.items.find((entry) => entry.id === id);
      if (item?.url) URL.revokeObjectURL(item.url);
      this.publish({ items: this.state.items.filter((entry) => entry.id !== id) });
    }
  }
  dispose() { this.submitting = false; this.capabilityRequest?.abort(); this.clear(); this.root = ""; }
}
