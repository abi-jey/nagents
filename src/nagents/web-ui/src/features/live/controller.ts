import type { Caption, LiveCreated, LiveEvent, LiveMedia, LiveSnapshot, LiveState, MediaHandlers } from "./types.js";

interface Dependencies {
  media(handlers: MediaHandlers): LiveMedia;
  create(sdp: string, voice: string, signal: AbortSignal, revision: string): Promise<LiveCreated>;
  read(id: string, after: number, signal: AbortSignal): Promise<LiveSnapshot>;
  close(id: string): Promise<LiveSnapshot>;
  pollMs?: number;
}

const initial = (): LiveState => ({
  phase: "idle", sessionId: "", error: "", notice: "", micMuted: false, outputMuted: false,
  playbackBlocked: false, startedAt: 0, endedAt: 0, captions: [],
});

export function liveFailure(cause: unknown): string {
  if (cause instanceof Error) {
    if (cause.name === "NotAllowedError" || cause.name === "SecurityError")
      return "Microphone access was denied. Allow it in your browser's site settings, then try again.";
    if (cause.name === "NotFoundError") return "No microphone found. Connect one, then try again.";
    if (cause.name === "NotReadableError") return "Your microphone is busy or unavailable. Check its connection and try again.";
    if (cause.name === "TimeoutError") return "The connection timed out. Check your network, then try again.";
    return cause.message;
  }
  return "Could not connect to GPT-Live. Try again.";
}

// Group adjacent fragments for readability, preserving exact text and timing.
// These are caption groups, not invented model turn boundaries.
export function appendCaptions(previous: Caption[], events: LiveEvent[]): Caption[] {
  const result = [...previous];
  for (const event of events) {
    if (event.type !== "transcript" || !event.text || !event.speaker) continue;
    const start = event.start_ms ?? 0, end = event.end_ms ?? start;
    const last = result.at(-1);
    if (last && last.speaker === event.speaker && start >= last.start && start - last.end < 1600 && last.text.length < 2000)
      result[result.length - 1] = { ...last, text: last.text + event.text, end: Math.max(last.end, end) };
    else result.push({ id: event.seq, speaker: event.speaker, text: event.text, start, end });
  }
  return result.slice(-200);
}

export class LiveController {
  private state = initial();
  private listeners = new Set<() => void>();
  private epoch = 0;
  private media?: LiveMedia;
  private abort?: AbortController;
  private timer?: ReturnType<typeof setTimeout>;
  private connectionTimer?: ReturnType<typeof setTimeout>;
  private cursor = 0;
  private endingDeadline = 0;
  private disposed = false;

  constructor(private readonly deps: Dependencies) {}
  getSnapshot = (): LiveState => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private update(change: Partial<LiveState>) {
    this.state = { ...this.state, ...change };
    if (!this.disposed) this.listeners.forEach((listener) => listener());
  }
  private stopMedia() {
    clearTimeout(this.connectionTimer);
    const media = this.media; this.media = undefined;
    media?.close();
  }
  private release() {
    clearTimeout(this.timer);
    this.abort?.abort(); this.abort = undefined;
    this.stopMedia();
  }
  private async closeRemote(id: string): Promise<{ snapshot?: LiveSnapshot; notice: string }> {
    if (!id) return { notice: "" };
    try {
      const snapshot = await this.deps.close(id);
      if (snapshot.session_id !== id) throw new Error("Mismatched closure");
      return { snapshot, notice: "" };
    } catch { return { notice: "Microphone and playback stopped. Server closure could not be confirmed; the abandoned session will expire automatically." }; }
  }
  private consume(snapshot: LiveSnapshot) {
    const events = snapshot.events.filter((event) => event.seq > this.cursor);
    this.cursor = Math.max(this.cursor, snapshot.cursor);
    const warning = events.filter((event) => event.type === "error").at(-1);
    this.update({ captions: appendCaptions(this.state.captions, events), ...(warning ? { notice: warning.message || warning.text || "A Live command was rejected." } : {}) });
  }
  private terminal(snapshot: LiveSnapshot): boolean {
    if (snapshot.status !== "closed" && snapshot.status !== "error") return false;
    ++this.epoch; this.release();
    this.update({ phase: snapshot.status === "error" ? "error" : "ended", endedAt: this.state.endedAt || Date.now(), playbackBlocked: false,
      error: snapshot.status === "error" ? snapshot.message || "Live finalization could not be confirmed." : "",
      notice: snapshot.status === "closed" ? snapshot.message || "Conversation ended. Your microphone is off." : "" });
    return true;
  }
  private ending(message: string) {
    this.stopMedia();
    this.endingDeadline ||= Date.now() + 45_000;
    this.update({ phase: "ending", notice: message, endedAt: this.state.endedAt || Date.now(), playbackBlocked: false });
  }
  private fail(message: string) {
    const id = this.state.sessionId;
    ++this.epoch; this.release();
    this.update({ phase: "error", error: message, endedAt: Date.now() });
    const epoch = this.epoch;
    void this.closeRemote(id).then(({ snapshot, notice }) => {
      if (epoch !== this.epoch) return;
      if (snapshot) this.consume(snapshot);
      if (notice || snapshot?.status === "error") this.update({ notice: notice || snapshot?.message || "Live finalization could not be confirmed." });
    });
  }

  async start(voice: string, revision: string): Promise<void> {
    if (this.disposed || ["permission", "connecting", "connected", "ending"].includes(this.state.phase)) return;
    const epoch = ++this.epoch;
    const abort = new AbortController(); this.abort = abort; this.cursor = 0; this.endingDeadline = 0;
    this.update({ ...initial(), phase: "permission" });
    try {
      const media = this.deps.media({
        connected: () => {
          if (epoch !== this.epoch || this.state.phase === "ending") return;
          clearTimeout(this.connectionTimer);
          this.update({ phase: "connected", startedAt: this.state.startedAt || Date.now() });
        },
        failed: (message) => { if (epoch === this.epoch) this.fail(message); },
        playbackBlocked: (blocked) => { if (epoch === this.epoch) this.update({ playbackBlocked: blocked }); },
      });
      this.media = media;
      const sdp = await media.offer(abort.signal);
      if (epoch !== this.epoch) return;
      this.update({ phase: "connecting" });
      // Keep awaiting a cancelled creation so a late-created server session can
      // be explicitly closed. The server lease covers a lost HTTP response.
      const session = await this.deps.create(sdp, voice, AbortSignal.timeout(70_000), revision);
      if (epoch !== this.epoch) { await this.closeRemote(session.session_id); return; }
      this.update({ sessionId: session.session_id });
      this.connectionTimer = setTimeout(() => {
        if (epoch === this.epoch) this.fail("The audio connection timed out. Check that your network allows WebRTC, then try again.");
      }, 25_000);
      await media.answer(session.sdp);
      if (epoch !== this.epoch) return;
      void this.poll(epoch, abort.signal);
    } catch (cause) {
      if (epoch === this.epoch) this.fail(liveFailure(cause));
    }
  }

  private async poll(epoch: number, signal: AbortSignal): Promise<void> {
    try {
      if (this.endingDeadline && Date.now() >= this.endingDeadline) {
        this.fail("Microphone and playback stopped, but Live finalization could not be confirmed."); return;
      }
      const snapshot = await this.deps.read(this.state.sessionId, this.cursor, AbortSignal.any([signal, AbortSignal.timeout(12_000)]));
      if (epoch !== this.epoch) return;
      if (snapshot.session_id !== this.state.sessionId) throw new Error("The Live session changed. Please reconnect.");
      this.consume(snapshot);
      if (this.terminal(snapshot)) return;
      if (snapshot.status === "closing") this.ending(snapshot.message || "Waiting for Live to finish closing…");
      this.timer = setTimeout(() => void this.poll(epoch, signal), this.deps.pollMs ?? 1000);
    } catch (cause) {
      if (epoch === this.epoch) this.fail(`Lost contact with ngn serve. ${liveFailure(cause)}`);
    }
  }

  async end(): Promise<void> {
    if (["idle", "ended", "ending"].includes(this.state.phase)) return;
    const epoch = ++this.epoch, id = this.state.sessionId;
    this.release();
    this.update({ phase: "ending", error: "", endedAt: Date.now(), playbackBlocked: false });
    const { snapshot, notice } = await this.closeRemote(id);
    if (epoch !== this.epoch) return;
    if (snapshot) {
      this.consume(snapshot);
      if (this.terminal(snapshot)) return;
      this.ending(snapshot.message || "Waiting for Live to finish closing…");
      const abort = new AbortController(); this.abort = abort;
      void this.poll(epoch, abort.signal);
    } else this.update({ phase: "ended", notice: notice || "Conversation ended. Your microphone is off." });
  }

  muteInput() {
    if (this.state.phase !== "connected") return;
    const muted = !this.state.micMuted;
    this.media?.muteInput(muted); this.update({ micMuted: muted });
  }
  muteOutput() {
    if (this.state.phase !== "connected") return;
    const muted = !this.state.outputMuted;
    this.media?.muteOutput(muted); this.update({ outputMuted: muted });
  }
  async play() {
    try { await this.media?.play(); }
    catch { this.update({ playbackBlocked: true }); }
  }
  dispose() {
    this.disposed = true; ++this.epoch; this.release();
    void this.closeRemote(this.state.sessionId);
    this.listeners.clear();
  }
}
