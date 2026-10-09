import type { AudioDeviceSelection, Caption, LiveCreated, LiveDelegation, LiveDelegationStatus, LiveEvent, LiveMedia, LiveSnapshot, LiveState, LiveTransport, MediaHandlers } from "./types.js";
import { readAudioDevices } from "./devices.js";
import { readVoiceContext } from "./context.js";
import { providerDelegationId } from "./identifiers.js";
import type { AudioSource } from "../../components/voiceSphere/useVoiceSphere.js";
import { silentSignal, type AudioFrame, type SignalFrame } from "../../components/voiceSphere/types.js";

interface Dependencies {
  media(handlers: MediaHandlers, transport: LiveTransport, devices: AudioDeviceSelection): LiveMedia;
  create(voice: string, signal: AbortSignal, revision: string, sessionId: string): Promise<LiveCreated>;
  token: string;
  read(id: string, after: number, signal: AbortSignal): Promise<LiveSnapshot>;
  close(id: string): Promise<LiveSnapshot>;
  pollMs?: number;
}

const initial = (): LiveState => ({
  phase: "idle", sessionId: "", error: "", notice: "", micMuted: false, outputMuted: false,
  playbackBlocked: false, startedAt: 0, endedAt: 0, captions: [], delegations: [], devices: readAudioDevices(),
  inputLevel: 0, outputLevel: 0, context: undefined,
});

const delegationStatuses: readonly LiveDelegationStatus[] = ["queued", "working", "completed", "failed", "cancelled"];
const activeDelegation = (value: LiveDelegation) => value.status === "queued" || value.status === "working";

export function currentLiveDelegation(delegations: readonly LiveDelegation[]): LiveDelegation | undefined {
  return delegations.reduce<LiveDelegation | undefined>((current, next) =>
    !current || (activeDelegation(next) && !activeDelegation(current)) ||
      (activeDelegation(next) === activeDelegation(current) && next.seq > current.seq) ? next : current, undefined);
}

function delegationRecord(value: unknown, sessionId: string, chatSessionId: string, cursor: number): LiveDelegation | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  if (!Number.isSafeInteger(cursor) || cursor < 0 ||
      typeof record.seq !== "number" || !Number.isSafeInteger(record.seq) || record.seq < 1 || record.seq > cursor ||
      !providerDelegationId(record.delegation_id) ||
      record.voice_session_id !== sessionId || !sessionId ||
      record.chat_session_id !== chatSessionId || !chatSessionId ||
      (record.type !== undefined && record.type !== "delegation") ||
      typeof record.status !== "string" || !delegationStatuses.some((status) => status === record.status) ||
      typeof record.agent !== "string" || !record.agent.trim() || typeof record.provider !== "string" ||
      typeof record.model !== "string" || typeof record.run_id !== "string" || typeof record.text !== "string") return undefined;
  if ((record.status === "working" || record.status === "completed") && !record.run_id.trim()) return undefined;
  return { id: record.delegation_id, sessionId, chatSessionId, seq: record.seq, status: record.status as LiveDelegationStatus,
    agent: record.agent, provider: record.provider, model: record.model, runId: record.run_id, text: record.text };
}

function mergeDelegations(previous: LiveDelegation[], snapshot: LiveSnapshot, events: LiveEvent[], chatSessionId: string): LiveDelegation[] {
  const states = new Map(previous.map((value) => [value.id, value]));
  // Current states recover handoffs whose lifecycle events fell out of the
  // bounded event window. Event-only snapshots remain backwards compatible.
  const records: unknown[] = [...(Array.isArray(snapshot.delegations) ? snapshot.delegations : []),
    ...events.filter((event) => event.type === "delegation")];
  for (const record of records) {
    const next = delegationRecord(record, snapshot.session_id, chatSessionId, snapshot.cursor);
    if (!next) continue;
    const current = states.get(next.id);
    if (current && (next.seq <= current.seq ||
        (!activeDelegation(current) && (next.status !== current.status || next.runId !== current.runId ||
          next.agent !== current.agent || next.provider !== current.provider || next.model !== current.model)) ||
        next.chatSessionId !== current.chatSessionId || (!!current.runId && next.runId !== current.runId) ||
        (current.status === "working" && next.status === "queued"))) continue;
    states.set(next.id, next);
  }
  const ordered = [...states.values()].sort((left, right) => left.seq - right.seq);
  // Keep ongoing work even while recent terminal handoffs rotate out.
  const active = ordered.filter(activeDelegation), terminal = ordered.filter((value) => !activeDelegation(value));
  const result = [...terminal.slice(-32), ...active].sort((left, right) => left.seq - right.seq);
  return result.length === previous.length && result.every((value, index) => value === previous[index]) ? previous : result;
}

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
  private chatSessionId = "";
  private endingDeadline = 0;
  private disposed = false;
  readonly audio: AudioSource = Object.freeze({ sample: (): AudioFrame => this.sampleAudio() });

  constructor(private readonly deps: Dependencies) {}
  private sampleAudio(): AudioFrame {
    const silent = (): AudioFrame => ({ input: silentSignal(), output: silentSignal() });
    const media = this.media, epoch = this.epoch;
    if (this.disposed || this.state.phase !== "connected" || !media?.sampleAudio) return silent();
    try {
      const frame = media.sampleAudio();
      if (this.disposed || epoch !== this.epoch || media !== this.media || this.state.phase !== "connected") return silent();
      const clean = (signal: SignalFrame, enabled: boolean): SignalFrame => {
        if (!enabled || !signal.active) return silentSignal();
        const level = (value: number) => Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
        return { active: true, rms: level(signal.rms), low: level(signal.low), mid: level(signal.mid), high: level(signal.high) };
      };
      return { input: clean(frame.input, !this.state.micMuted), output: clean(frame.output, !this.state.outputMuted && !this.state.playbackBlocked) };
    } catch { return silent(); }
  }
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
    if (this.state.inputLevel || this.state.outputLevel) this.update({ inputLevel: 0, outputLevel: 0 });
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
    const stale = snapshot.cursor < this.cursor;
    const events = snapshot.events.filter((event) => event.seq > this.cursor);
    this.cursor = Math.max(this.cursor, snapshot.cursor);
    const warning = events.filter((event) => event.type === "error").at(-1);
    this.update({ captions: appendCaptions(this.state.captions, events), delegations: stale ? this.state.delegations : mergeDelegations(this.state.delegations, snapshot, events, this.chatSessionId),
      context: stale ? this.state.context : readVoiceContext(snapshot.context, this.chatSessionId) || this.state.context,
      ...(warning?.type === "error" ? { notice: warning.message || warning.text || "A Live command was rejected." } : {}) });
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
    this.update({ phase: "error", error: message, endedAt: Date.now(), playbackBlocked: false });
    const epoch = this.epoch;
    void this.closeRemote(id).then(({ snapshot, notice }) => {
      if (epoch !== this.epoch) return;
      if (snapshot) this.consume(snapshot);
      if (notice || snapshot?.status === "error") this.update({ notice: notice || snapshot?.message || "Live finalization could not be confirmed." });
    });
  }

  async start(voice: string, revision: string, sessionId = "", transport: LiveTransport = "websocket"): Promise<void> {
    if (this.disposed || ["permission", "connecting", "connected", "ending"].includes(this.state.phase)) return;
    const epoch = ++this.epoch;
    const abort = new AbortController(); this.abort = abort; this.cursor = 0; this.endingDeadline = 0;
    this.chatSessionId = sessionId;
    this.update({ ...initial(), phase: "permission" });
    try {
      if (transport !== "websocket") throw new Error("Voice must connect through the ngn server relay.");
      const media = this.deps.media({
        connected: () => {
          if (epoch !== this.epoch || this.state.phase === "ending") return;
          clearTimeout(this.connectionTimer);
          this.update({ phase: "connected", startedAt: this.state.startedAt || Date.now() });
        },
        ended: () => { if (epoch === this.epoch) void this.end(); },
        failed: (message) => { if (epoch === this.epoch) this.fail(message); },
        playbackBlocked: (blocked) => { if (epoch === this.epoch) this.update({ playbackBlocked: blocked, ...(blocked ? { outputLevel: 0 } : {}) }); },
        levels: (input, output) => {
          if (epoch !== this.epoch || this.state.phase !== "connected" || this.disposed) return;
          const level = (value: number) => Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
          const inputLevel = this.state.micMuted ? 0 : level(input);
          const outputLevel = this.state.outputMuted || this.state.playbackBlocked ? 0 : level(output);
          if (inputLevel !== this.state.inputLevel || outputLevel !== this.state.outputLevel)
            this.update({ inputLevel, outputLevel });
        },
      }, transport, this.state.devices);
      this.media = media;
      await media.prepare(abort.signal);
      if (epoch !== this.epoch) return;
      this.update({ phase: "connecting" });
      // Keep awaiting a cancelled creation so a late-created server session can
      // be explicitly closed. The server lease covers a lost HTTP response.
      const session = await this.deps.create(voice, AbortSignal.timeout(70_000), revision, sessionId);
      if (epoch !== this.epoch) { await this.closeRemote(session.session_id); return; }
      this.update({ sessionId: session.session_id, context: readVoiceContext(session.context, sessionId) });
      this.connectionTimer = setTimeout(() => {
        if (epoch === this.epoch) this.fail("The voice connection timed out. Check your network, then try again.");
      }, 25_000);
      await media.connect(session.session_id, this.deps.token);
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
    // Provider media and finalization belong to ngn. Browser devices and its
    // local audio socket can stop immediately while the server finishes.
    this.release();
    this.update({ phase: "ending", error: "", endedAt: Date.now(), playbackBlocked: false });
    const { snapshot, notice } = await this.closeRemote(id);
    if (epoch !== this.epoch) return;
    if (snapshot) {
      this.consume(snapshot);
      if (this.terminal(snapshot)) return;
      this.ending(snapshot.message || "Waiting for Live to finish closing…");
      const abort = this.abort || new AbortController(); this.abort = abort;
      void this.poll(epoch, abort.signal);
    } else {
      this.release();
      this.update({ phase: "ended", notice: notice || "Conversation ended. Your microphone is off." });
    }
  }

  async switchDevice(kind: "input" | "output", id: string): Promise<void> {
    const media = this.media, epoch = this.epoch;
    if (this.disposed || this.state.phase !== "connected" || !media)
      throw new Error("Voice is not connected. Choose your device before starting again.");
    const key = kind === "input" ? "inputId" : "outputId";
    if (this.state.devices[key] === id) return;
    if (kind === "input") await media.setInputDevice(id);
    else await media.setOutputDevice(id);
    if (this.disposed || epoch !== this.epoch || this.media !== media || this.state.phase !== "connected")
      throw new DOMException("Voice ended while the device was changing.", "AbortError");
    this.update({ devices: { ...this.state.devices, [key]: id } });
  }

  muteInput() {
    if (this.state.phase !== "connected") return;
    const muted = !this.state.micMuted;
    this.media?.muteInput(muted); this.update({ micMuted: muted, ...(muted ? { inputLevel: 0 } : {}) });
  }
  muteOutput() {
    if (this.state.phase !== "connected") return;
    const muted = !this.state.outputMuted;
    this.media?.muteOutput(muted); this.update({ outputMuted: muted, ...(muted ? { outputLevel: 0 } : {}) });
  }
  async play() {
    const epoch = this.epoch;
    try { await this.media?.play(); }
    catch { if (epoch === this.epoch) this.update({ playbackBlocked: true, outputLevel: 0 }); }
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.listeners.clear();
    const alreadyEnding = this.state.phase === "ending";
    ++this.epoch; this.release();
    if (!alreadyEnding) void this.closeRemote(this.state.sessionId);
  }
}
