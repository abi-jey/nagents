import { request } from "../../api/client.js";

export type DeliveryAsset = { asset_id: string; filename: string; media_type: string; byte_length: number; position: number };
export type LocalDelivery = {
  delivery_id: string; session_id: string; channel: string; text: string; sequence: number;
  anchor_message_id: string; call_position: number; earlier: boolean; assets: DeliveryAsset[];
};
export const MEDIA_TYPES = new Set([
  "image/png", "image/jpeg", "image/gif", "image/webp", "audio/mpeg", "audio/wav", "audio/ogg", "video/mp4", "video/webm",
]);
const MAX_FILE = 20 * 1024 * 1024;

// Reservations include in-flight bytes and retained object URLs across all cards.
// A full budget is an explicit retryable UI state, never an unbounded fetch queue.
export class MediaBudget {
  private bytes = 0;
  private fetching = 0;
  constructor(private limit = 60 * 1024 * 1024, private concurrency = 2) {}
  reserve(size: number) {
    if (!Number.isSafeInteger(size) || size <= 0 || size > MAX_FILE) throw new Error("Invalid media size.");
    if (this.fetching >= this.concurrency || this.bytes + size > this.limit)
      throw new Error("Media budget is full. Unload another preview or retry when loading finishes.");
    this.bytes += size; this.fetching++;
    let loading = true, released = false;
    const loaded = () => { if (loading) { loading = false; this.fetching--; } };
    return { loaded, release: () => { if (!released) { released = true; loaded(); this.bytes -= size; } } };
  }
}
export const mediaBudget = new MediaBudget();

export async function loadAsset(token: string, delivery: LocalDelivery, asset: DeliveryAsset, signal: AbortSignal,
  budget = mediaBudget): Promise<{ url: string; release: () => void }> {
  if (!MEDIA_TYPES.has(asset.media_type)) throw new Error("This attachment type cannot be previewed.");
  const reservation = budget.reserve(asset.byte_length);
  let url = "";
  try {
    signal.throwIfAborted();
    const path = ["sessions", delivery.session_id, "deliveries", delivery.delivery_id, "assets", asset.asset_id]
      .map(encodeURIComponent).join("/");
    const response = await request(path, token, undefined, signal);
    if (response.headers.get("Content-Type") !== asset.media_type ||
        (response.headers.has("Content-Length") && Number(response.headers.get("Content-Length")) !== asset.byte_length)) {
      await response.body?.cancel();
      throw new Error("Media metadata changed. Reload the conversation.");
    }
    if (!response.body) throw new Error("Media response is empty.");
    const reader = response.body.getReader();
    const chunks: Uint8Array<ArrayBuffer>[] = [];
    let size = 0;
    try {
      while (true) {
        signal.throwIfAborted();
        const { value, done } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > asset.byte_length || size > MAX_FILE) throw new Error("Media response exceeded its byte limit.");
        chunks.push(new Uint8Array(value));
      }
      signal.throwIfAborted();
      if (size !== asset.byte_length) throw new Error("Media response was incomplete.");
      url = URL.createObjectURL(new Blob(chunks, { type: asset.media_type }));
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
    reservation.loaded();
    let released = false;
    return { url, release: () => { if (!released) { released = true; URL.revokeObjectURL(url); reservation.release(); } } };
  } catch (error) {
    if (url) URL.revokeObjectURL(url);
    reservation.release();
    throw error;
  }
}
