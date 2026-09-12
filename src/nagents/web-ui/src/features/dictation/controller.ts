import { RequestError } from "../../api/client.js";
import type {
  DictationConfig, DictationContext, DictationState, RecordedAudio, Recording, RecordingCallbacks,
} from "./types.js";

export type DictationDependencies = {
  record: (config: DictationConfig, callbacks: RecordingCallbacks) => Recording;
  transcribe: (context: DictationContext, audio: Blob, signal: AbortSignal) => Promise<string>;
};

export function dictationFailure(cause: unknown): string {
  if (cause instanceof Error && cause.name === "NotAllowedError")
    return "Microphone permission was denied. Allow microphone access in your browser's site settings to try again, or keep typing.";
  if (cause instanceof Error && cause.name === "NotFoundError")
    return "No microphone is available. Connect one to try again, or keep typing.";
  if (cause instanceof Error && cause.name === "NotReadableError")
    return "The microphone could not be opened. Check whether another app is using it, or keep typing.";
  if (cause instanceof RequestError && cause.status === 409)
    return `${cause.message} Your draft is kept. Finish active work or refresh settings before recording again. No request was retried.`;
  return `${cause instanceof Error ? cause.message : "Dictation failed."} Your draft is kept. No request was retried.`;
}

// No chat submission callback exists here. Only an explicit insert may hand text
// to the draft owner, and a separate normal Send can start a run.
export class DictationController {
  private state: DictationState = { phase: "idle", message: "" };
  private readonly listeners = new Set<() => void>();
  private generation = 0;
  private recording?: Recording;
  private audio?: RecordedAudio;
  private context?: DictationContext;
  private upload?: AbortController;

  constructor(private readonly dependencies: DictationDependencies) {}

  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  get active() { return this.state.phase !== "idle" && this.state.phase !== "error"; }
  private receive(state: DictationState) {
    this.state = state;
    for (const listener of this.listeners) listener();
  }

  cancel = (message = "Dictation discarded. Your message draft is kept.") => {
    this.generation++;
    this.recording?.cancel();
    this.upload?.abort();
    this.recording = undefined;
    this.upload = undefined;
    this.audio = undefined;
    this.context = undefined;
    this.receive({ phase: "idle", message });
  };

  visibilityChanged = (hidden: boolean) => {
    // Completed review is ordinary editable text, with no capture or upload to
    // interrupt. Keep it across tab switches; ownership cleanup still cancels it.
    if (hidden && this.active && this.state.phase !== "review")
      this.cancel("Dictation cancelled when leaving the page. Your draft is kept.");
  };

  private fail(cause: unknown) {
    this.cancel("");
    this.receive({ phase: "error", message: dictationFailure(cause) });
  }

  start = async (context: DictationContext) => {
    if (this.active) return;
    if (!context.token || !context.sessionId || !context.config.revision ||
        !context.config.available || !context.config.enabled || !context.config.admin_enabled ||
        context.config.content_type !== "audio/wav" || context.config.sample_rate !== 16000 ||
        context.config.channels !== 1 || context.config.sample_width !== 2) {
      this.receive({ phase: "error", message: "Dictation is unavailable. Check Settings, or keep typing." });
      return;
    }
    const generation = ++this.generation;
    this.context = { ...context, config: { ...context.config } };
    this.receive({ phase: "permission" });
    try {
      this.recording = this.dependencies.record(this.context.config, {
        stopped: (audio) => {
          if (generation !== this.generation || !this.recording) return;
          // Only the user's Stop action authorizes an immediate upload. A cap
          // needs a separate Transcribe action; duplicate/unsolicited callbacks
          // must never start another provider request or replace reviewed text.
          if (!audio.limited && this.state.phase !== "stopping") {
            this.fail(new Error("Recording ended before Stop was selected. Audio was discarded."));
            return;
          }
          this.recording = undefined;
          this.audio = audio;
          if (audio.limited) this.receive({ phase: "recorded", seconds: audio.seconds });
          else void this.transcribe();
        },
        failed: (cause) => {
          if (generation === this.generation && this.recording) this.fail(cause);
        },
      });
      await this.recording.start();
      if (generation === this.generation && this.state.phase === "permission")
        this.receive({ phase: "recording" });
    } catch (cause) { if (generation === this.generation) this.fail(cause); }
  };

  stop = () => {
    if (this.state.phase !== "recording") return;
    this.receive({ phase: "stopping" });
    this.recording?.stop();
  };

  transcribe = async () => {
    if (!this.audio || !this.context || this.upload) return;
    const generation = this.generation;
    const controller = new AbortController();
    this.upload = controller;
    this.receive({ phase: "transcribing" });
    try {
      const text = await this.dependencies.transcribe(this.context, this.audio.blob, controller.signal);
      if (generation !== this.generation || controller.signal.aborted) return;
      this.audio = undefined;
      this.receive({ phase: "review", text, error: "" });
    } catch (cause) {
      if (generation === this.generation && !controller.signal.aborted) this.fail(cause);
    } finally { if (this.upload === controller) this.upload = undefined; }
  };

  edit = (text: string) => {
    if (this.state.phase === "review") this.receive({ phase: "review", text, error: "" });
  };

  insert = (accept: (text: string) => string): boolean => {
    if (this.state.phase !== "review") return false;
    const error = accept(this.state.text);
    if (error) {
      this.receive({ ...this.state, error });
      return false;
    }
    this.cancel("Transcription inserted. Review your message, then choose Send.");
    return true;
  };
}
