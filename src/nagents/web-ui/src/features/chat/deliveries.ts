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
const MAX_WAITING = 16;

class MediaLoadingBusy extends Error {}

// Reservations include in-flight bytes and retained object URLs across all cards.
// Automatic previews may wait for a fetch slot, with a bounded, abortable queue.
// Exhausted retained-byte capacity remains an explicit retryable UI state.
export class MediaBudget {
  private bytes = 0;
  private fetching = 0;
  private waiting = new Set<() => void>();
  constructor(private limit = 60 * 1024 * 1024, private concurrency = 2) {}
  private wake() {
    const callbacks = [...this.waiting];
    for (const callback of callbacks) callback();
  }
  async reserveWhenReady(size: number, signal: AbortSignal): Promise<ReturnType<MediaBudget["reserve"]>> {
    while (true) {
      signal.throwIfAborted();
      try { return this.reserve(size); }
      catch (error) {
        if (!(error instanceof MediaLoadingBusy)) throw error;
        if (this.waiting.size >= MAX_WAITING) throw new Error("Too many previews are loading. Retry shortly.");
        await new Promise<void>((resolve, reject) => {
          const cleanup = () => { this.waiting.delete(ready); signal.removeEventListener("abort", abort); };
          const ready = () => { cleanup(); resolve(); };
          const abort = () => { cleanup(); reject(signal.reason); };
          this.waiting.add(ready); signal.addEventListener("abort", abort, { once: true });
          if (signal.aborted) abort();
        });
      }
    }
  }
  reserve(size: number) {
    if (!Number.isSafeInteger(size) || size <= 0 || size > MAX_FILE) throw new Error("Invalid media size.");
    if (this.bytes + size > this.limit)
      throw new Error("Media budget is full. Unload another preview or retry when loading finishes.");
    if (this.fetching >= this.concurrency)
      throw new MediaLoadingBusy("Media budget is full. Retry when loading finishes.");
    this.bytes += size; this.fetching++;
    let loading = true, released = false;
    const loaded = () => { if (loading) { loading = false; this.fetching--; this.wake(); } };
    return { loaded, release: () => { if (!released) { released = true; this.bytes -= size; loaded(); this.wake(); } } };
  }
}
export const mediaBudget = new MediaBudget();

export async function loadAsset(token: string, delivery: LocalDelivery, asset: DeliveryAsset, signal: AbortSignal,
  budget = mediaBudget, waitForSlot = false): Promise<{ url: string; release: () => void }> {
  const path = ["sessions", delivery.session_id, "deliveries", delivery.delivery_id, "assets", asset.asset_id]
    .map(encodeURIComponent).join("/");
  return loadMedia(token, path, asset, signal, budget, waitForSlot);
}

export async function loadMedia(token: string, path: string, asset: Pick<DeliveryAsset, "media_type" | "byte_length">,
  signal: AbortSignal, budget = mediaBudget, waitForSlot = false): Promise<{ url: string; release: () => void }> {
  if (!MEDIA_TYPES.has(asset.media_type)) throw new Error("This attachment type cannot be previewed.");
  const reservation = waitForSlot ? await budget.reserveWhenReady(asset.byte_length, signal) : budget.reserve(asset.byte_length);
  let url = "";
  try {
    signal.throwIfAborted();
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
