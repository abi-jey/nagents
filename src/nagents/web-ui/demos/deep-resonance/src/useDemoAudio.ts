import { useCallback, useEffect, useRef, useState, type RefCallback } from "react";
import { silentSignal, type AudioFrame, type SignalFrame } from "./types.js";
import { createPcmAudio } from "./audioStreams.js";

export interface DemoAudioOptions {
  sampleUrl: string;
  onPlaybackStart(): void;
  onMicStart(): void;
}

export interface DemoInputDevice { id: string; label: string }
export interface DemoInputState {
  status: string;
  active: boolean;
  starting: boolean;
  deviceId: string;
  devices: readonly DemoInputDevice[];
  level: number;
}
export interface DemoOutputState { status: string; fileName: string; playing: boolean }
interface AudioView { input: DemoInputState; output: DemoOutputState }
type ViewListener = (view: AudioView) => void;

export interface DemoAudio {
  audioRef: RefCallback<HTMLAudioElement>;
  input: DemoInputState;
  output: DemoOutputState;
  toggleMicrophone(): Promise<void>;
  setInputDevice(id: string): Promise<void>;
  playSample(): Promise<void>;
  chooseFile(file: File): void;
  pauseOutput(): void;
  stopMicrophone(): void;
  sample(): AudioFrame;
}

interface AnalysisGraph {
  context: AudioContext;
  analyser: AnalyserNode;
  samples: Float32Array<ArrayBuffer>;
  bytes: Uint8Array<ArrayBuffer>;
  bus: ReturnType<typeof createPcmAudio>;
  channel: "input" | "output";
  queued: boolean;
  gain: GainNode;
}
interface OutputGraph extends AnalysisGraph {
  source: MediaElementAudioSourceNode;
  element: HTMLAudioElement;
}
interface InputGraph extends AnalysisGraph {
  stream?: MediaStream;
  source?: MediaStreamAudioSourceNode;
}
const nativeLittleEndian = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;

function clearSignal(signal: SignalFrame): void {
  signal.active = false; signal.rms = 0; signal.low = 0; signal.mid = 0; signal.high = 0;
}

function analyse(graph: AnalysisGraph, signal: SignalFrame): void {
  graph.analyser.getFloatTimeDomainData(graph.samples);
  if (!nativeLittleEndian) {
    const bytes = new DataView(graph.bytes.buffer, graph.bytes.byteOffset, graph.bytes.byteLength);
    graph.samples.forEach((value, index) => bytes.setFloat32(index * 4, value, true));
  }
  const at = performance.now() / 1000 - graph.samples.length / graph.context.sampleRate;
  graph.bus[graph.channel].write(graph.bytes, { at });
  graph.queued = true;
  Object.assign(signal, graph.bus.sample()[graph.channel]);
}

function resetAnalysis(graph?: AnalysisGraph): void {
  if (graph?.queued) { graph.bus[graph.channel].reset(); graph.queued = false; }
}

function createContext(): AudioContext {
  const browser = globalThis as typeof globalThis & { webkitAudioContext?: typeof AudioContext };
  const Audio = browser.AudioContext || browser.webkitAudioContext;
  if (!Audio) throw new Error("Web Audio is unavailable.");
  return new Audio();
}

function createAnalysis(context: AudioContext, gainValue: number, channel: "input" | "output"): AnalysisGraph {
  const analyser = context.createAnalyser(), gain = context.createGain();
  analyser.fftSize = 1024; analyser.smoothingTimeConstant = 0; gain.gain.value = gainValue;
  const samples = new Float32Array(analyser.fftSize);
  const bytes = nativeLittleEndian ? new Uint8Array(samples.buffer) : new Uint8Array(samples.byteLength);
  const format = { encoding: "f32le" as const, sampleRate: context.sampleRate, channels: 1 as const };
  const bus = createPcmAudio({ inputFormat: format, outputFormat: format, clock: () => performance.now() / 1000, maxBufferedSeconds: 2 });
  return { context, analyser, gain, samples, bytes, bus, channel, queued: false };
}

function disconnect(node?: AudioNode): void {
  try { node?.disconnect(); } catch { /* A closed context may have retired the node. */ }
}
function closeContext(context: AudioContext): void {
  context.onstatechange = null;
  if (context.state !== "closed") void context.close().catch(() => {});
}
function stopTracks(stream?: MediaStream): void { stream?.getTracks().forEach(track => track.stop()); }

/** Instance-owned resources. Rendering reads numbers, never saved audio frames. */
class DemoAudioController {
  view: AudioView = {
    input: { status: "Microphone off · can mix with every state", active: false, starting: false, deviceId: "", devices: [{ id: "", label: "System default" }], level: 0 },
    output: { status: "Synthetic demo voice", fileName: "Sample speech", playing: false },
  };
  private readonly frame: AudioFrame = { input: silentSignal(), output: silentSignal() };
  private listener?: ViewListener;
  private mounted = false;
  private lifecycle = 0;
  private mediaEpoch = 0;
  private mediaAttached = false;
  private media?: HTMLAudioElement;
  private listeningMedia?: HTMLAudioElement;
  private outputGraph?: OutputGraph;
  private inputGraph?: InputGraph;
  private inputGeneration = 0;
  private outputGeneration = 0;
  private waiting = false;
  private blobUrl = "";
  private authorized = false;
  private meterTime = -Infinity;

  constructor(private readonly options: () => DemoAudioOptions) {}

  private input(patch: Partial<DemoInputState>): void {
    this.view = { ...this.view, input: { ...this.view.input, ...patch } };
    if (this.mounted) this.listener?.(this.view);
  }
  private output(patch: Partial<DemoOutputState>): void {
    this.view = { ...this.view, output: { ...this.view.output, ...patch } };
    if (this.mounted) this.listener?.(this.view);
  }

  mount(listener: ViewListener): () => void {
    this.mounted = true; this.lifecycle++; this.listener = listener;
    this.bindEvents(); listener(this.view);
    window.addEventListener("pagehide", this.pageHide);
    navigator.mediaDevices?.addEventListener?.("devicechange", this.deviceChange);
    return () => {
      this.mounted = false; this.listener = undefined;
      const generation = ++this.lifecycle;
      this.unbindEvents(); window.removeEventListener("pagehide", this.pageHide);
      navigator.mediaDevices?.removeEventListener?.("devicechange", this.deviceChange);
      this.stopMicrophone(); this.pauseOutput();
      // React StrictMode replays effects synchronously. Do not close and then
      // recreate a MediaElementSource for the same surviving audio element.
      queueMicrotask(() => {
        if (!this.mounted && generation === this.lifecycle) { this.closeOutput(); this.releaseBlob(); }
      });
    };
  }

  attach = (element: HTMLAudioElement | null): void => {
    const epoch = ++this.mediaEpoch;
    if (!element) {
      this.mediaAttached = false; this.unbindEvents(); this.pauseOutput();
      queueMicrotask(() => {
        if (!this.mediaAttached && epoch === this.mediaEpoch) { this.closeOutput(); this.media = undefined; }
      });
      return;
    }
    if (element !== this.media) {
      this.unbindEvents(); this.pauseOutput(); this.closeOutput(); this.media = element;
    }
    this.mediaAttached = true;
    const src = this.blobUrl || this.options().sampleUrl;
    if (element.getAttribute("src") !== src) element.src = src;
    if (this.mounted) this.bindEvents();
  };

  private readonly events: readonly [keyof HTMLMediaElementEventMap, EventListener][] = [
    ["play", () => { void this.beginPlayback(); }],
    ["playing", () => { this.waiting = false; this.output({ playing: true, status: this.playingLabel() }); }],
    ["waiting", () => { this.waiting = true; this.clearOutputFrame(); this.output({ status: "Loading audio…" }); }],
    ["seeking", () => { this.clearOutputFrame(); }],
    ["seeked", () => { this.waiting = false; }],
    ["pause", () => { this.clearOutputFrame(); this.output({ playing: false, status: this.media?.ended ? "Finished · play again to compare" : "Paused" }); }],
    ["ended", () => { this.clearOutputFrame(); this.output({ playing: false, status: "Finished · play again to compare" }); }],
    ["emptied", () => { this.waiting = false; this.clearOutputFrame(); }],
    ["volumechange", () => { if (this.media?.muted || this.media?.volume === 0) this.clearOutputFrame(); }],
    ["error", () => { this.failPlayback(); }],
  ];

  private bindEvents(): void {
    if (!this.mediaAttached || !this.media || this.listeningMedia === this.media) return;
    this.unbindEvents(); this.listeningMedia = this.media;
    for (const [name, handler] of this.events) this.media.addEventListener(name, handler);
  }
  private unbindEvents(): void {
    if (this.listeningMedia) for (const [name, handler] of this.events) this.listeningMedia.removeEventListener(name, handler);
    this.listeningMedia = undefined;
  }
  private clearOutputFrame(): void { resetAnalysis(this.outputGraph); clearSignal(this.frame.output); }
  private clearInputFrame(): void { resetAnalysis(this.inputGraph); clearSignal(this.frame.input); }

  private playingLabel(): string { return this.blobUrl ? "Playing local audio" : "Synthetic demo voice · playing"; }

  private ensureOutput(): OutputGraph {
    if (!this.media || !this.mediaAttached) throw new Error("The audio player is unavailable.");
    if (this.outputGraph?.element === this.media) return this.outputGraph;
    const context = createContext();
    try {
      const analysis = createAnalysis(context, 1, "output"), source = context.createMediaElementSource(this.media);
      // Native volume/mute already attenuate MediaElementSource. A unity bus
      // avoids applying that attenuation twice, and analysis follows the bus.
      source.connect(analysis.gain).connect(analysis.analyser).connect(context.destination);
      const graph: OutputGraph = { ...analysis, source, element: this.media };
      this.outputGraph = graph; this.outputGeneration++;
      context.onstatechange = () => {
        if (this.outputGraph !== graph || context.state === "running") return;
        const wasPlaying = !graph.element.paused;
        this.pauseOutput();
        if (wasPlaying) this.output({ status: "Playback paused by the browser · press Play" });
      };
      return graph;
    } catch (error) { closeContext(context); throw error; }
  }

  private async beginPlayback(): Promise<void> {
    if (!this.mounted || !this.mediaAttached || !this.media) return;
    const element = this.media;
    this.options().onPlaybackStart(); this.output({ playing: true, status: this.playingLabel() });
    try {
      const graph = this.ensureOutput(), generation = this.outputGeneration;
      await graph.context.resume();
      if (!this.mounted || generation !== this.outputGeneration || element !== this.media || element?.paused) return;
      this.waiting = element.readyState < 2;
      this.output({ playing: true, status: this.playingLabel() });
    } catch { if (this.mounted && element === this.media) this.failPlayback(); }
  }

  playSample = async (): Promise<void> => {
    if (!this.mounted || !this.mediaAttached || !this.media) return;
    const element = this.media;
    this.pauseOutput();
    const previous = this.blobUrl; this.blobUrl = "";
    if (element.getAttribute("src") !== this.options().sampleUrl) { element.src = this.options().sampleUrl; element.load(); }
    if (previous) URL.revokeObjectURL(previous);
    this.output({ fileName: "Sample speech", status: "Synthetic demo voice" });
    element.currentTime = 0;
    try {
      const graph = this.ensureOutput();
      await Promise.all([graph.context.resume(), element.play()]);
    } catch { if (this.mounted && element === this.media) this.failPlayback(); }
  };

  chooseFile = (file: File): void => {
    if (!this.mounted || !this.media) return;
    this.pauseOutput();
    const previous = this.blobUrl; this.blobUrl = URL.createObjectURL(file);
    this.media.src = this.blobUrl; this.media.load();
    if (previous) URL.revokeObjectURL(previous);
    this.output({ fileName: file.name, playing: false, status: "Local audio · stays on this device" });
  };
  pauseOutput = (): void => {
    this.clearOutputFrame();
    if (this.media && !this.media.paused) this.media.pause();
    if (this.view.output.playing) this.output({ playing: false, status: "Paused" });
  };
  private failPlayback(): void {
    this.pauseOutput(); this.output({ playing: false, status: "Couldn’t play this audio. Try Play or choose another file." });
  }
  private closeOutput(): void {
    this.outputGeneration++;
    const graph = this.outputGraph; this.outputGraph = undefined;
    if (graph) {
      graph.context.onstatechange = null; disconnect(graph.source); disconnect(graph.gain); disconnect(graph.analyser);
      graph.samples.fill(0); graph.bus.dispose(); closeContext(graph.context);
    }
    this.clearOutputFrame();
  }
  private releaseBlob(): void { if (this.blobUrl) URL.revokeObjectURL(this.blobUrl); this.blobUrl = ""; }

  stopMicrophone = (): void => { this.stopInput("Microphone off · can mix with every state"); };
  private stopInput(status: string): void {
    this.inputGeneration++;
    const graph = this.inputGraph; this.inputGraph = undefined;
    if (graph) {
      graph.context.onstatechange = null; stopTracks(graph.stream);
      disconnect(graph.source); disconnect(graph.analyser); disconnect(graph.gain);
      graph.samples.fill(0); graph.bus.dispose(); closeContext(graph.context);
    }
    this.clearInputFrame();
    this.input({ active: false, starting: false, level: 0, status });
  }

  toggleMicrophone = async (): Promise<void> => {
    if (this.view.input.starting || this.inputGraph?.stream) this.stopMicrophone();
    else await this.startInput(this.view.input.deviceId);
  };
  setInputDevice = async (id: string): Promise<void> => {
    const restart = this.view.input.starting || Boolean(this.inputGraph?.stream);
    this.input({ deviceId: id });
    if (restart) await this.startInput(id);
  };

  private async startInput(deviceId: string): Promise<void> {
    if (!this.mounted) return;
    this.stopMicrophone(); this.input({ deviceId });
    const media = navigator.mediaDevices;
    if (globalThis.isSecureContext === false || !media?.getUserMedia) {
      this.input({ status: "Microphone unavailable here · use localhost or HTTPS" }); return;
    }
    const generation = ++this.inputGeneration;
    this.input({ starting: true, status: "Waiting for permission · local only" });
    let context: AudioContext | undefined;
    try {
      context = createContext();
      const graph: InputGraph = createAnalysis(context, 0, "input"); this.inputGraph = graph;
      graph.analyser.connect(graph.gain).connect(context.destination);
      // Both requests begin in the explicit user gesture. Assign a resolved
      // stream immediately so Stop can release it even if resume is pending.
      const resumed = context.resume();
      const incoming = media.getUserMedia({ audio: {
        echoCancellation: true, noiseSuppression: true, autoGainControl: false,
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      } }).then(stream => {
        if (!this.mounted || generation !== this.inputGeneration) { stopTracks(stream); return undefined; }
        graph.stream = stream; return stream;
      });
      const [stream] = await Promise.all([incoming, resumed]);
      if (!this.mounted || generation !== this.inputGeneration) { stopTracks(stream); return; }
      if (!stream?.getAudioTracks().length || stream.getAudioTracks().every(track => track.readyState === "ended")) throw new Error("No microphone audio.");
      if (context.state !== "running") throw new Error("Microphone context did not start.");
      graph.source = context.createMediaStreamSource(stream); graph.source.connect(graph.analyser);
      for (const track of stream.getAudioTracks()) {
        track.addEventListener("ended", () => { if (generation === this.inputGeneration) this.stopInput("Microphone disconnected · start again to retry"); });
        track.addEventListener("mute", () => {
          if (generation === this.inputGeneration) { this.clearInputFrame(); this.input({ level: 0, status: "Microphone temporarily muted · local only" }); }
        });
        track.addEventListener("unmute", () => { if (generation === this.inputGeneration) this.input({ status: "Listening · local only" }); });
      }
      context.onstatechange = () => {
        if (generation === this.inputGeneration && graph.context.state !== "running") this.stopInput("Microphone paused by the browser · start again");
      };
      this.authorized = true; this.input({ starting: false, active: true, status: "Listening · local only" });
      this.options().onMicStart(); void this.refreshDevices(generation);
    } catch (error: unknown) {
      // A context can fail before the analysis graph was assigned.
      if (context && this.inputGraph?.context !== context) closeContext(context);
      if (!this.mounted || generation !== this.inputGeneration) return;
      const name = error && typeof error === "object" && "name" in error && typeof error.name === "string" ? error.name : "";
      const status = name === "NotAllowedError" || name === "SecurityError" ? "Microphone access denied · allow access and try again"
        : name === "NotFoundError" || name === "OverconstrainedError" ? "Microphone unavailable · choose another input"
          : name === "NotReadableError" ? "Microphone busy · close other users and try again" : "Microphone could not start · try again";
      this.stopInput(status);
    }
  }

  private async refreshDevices(generation = this.inputGeneration): Promise<void> {
    const media = navigator.mediaDevices;
    if (!this.authorized || !media?.enumerateDevices) return;
    try {
      const listed = await media.enumerateDevices();
      if (!this.mounted || generation !== this.inputGeneration) return;
      const seen = new Set(["", "default", "communications"]), devices: DemoInputDevice[] = [{ id: "", label: "System default" }];
      for (const device of listed) {
        if (device.kind !== "audioinput" || seen.has(device.deviceId)) continue;
        seen.add(device.deviceId); devices.push({ id: device.deviceId, label: device.label || `Microphone ${devices.length}` });
      }
      const deviceId = seen.has(this.view.input.deviceId) ? this.view.input.deviceId : "";
      this.input({ devices, deviceId });
    } catch { /* Missing device labels never interrupt a working stream. */ }
  }
  private deviceChange = (): void => { if (this.mounted && this.authorized) void this.refreshDevices(); };

  sample = (): AudioFrame => {
    const input = this.inputGraph, output = this.outputGraph, media = this.media;
    const inputLive = this.mounted && input?.context.state === "running" && input.stream?.getAudioTracks().some(track => track.readyState === "live" && track.enabled && !track.muted);
    try { if (inputLive && input && !this.view.input.starting) analyse(input, this.frame.input); else this.clearInputFrame(); }
    catch { this.stopInput("Microphone analysis stopped · start again"); }
    const outputLive = this.mounted && output?.context.state === "running" && media && !media.paused && !media.ended && !media.seeking && !this.waiting && media.readyState >= 2 && !media.muted && media.volume > 0;
    try { if (outputLive && output) analyse(output, this.frame.output); else this.clearOutputFrame(); }
    catch { this.clearOutputFrame(); }
    const now = performance.now();
    if (this.mounted && now - this.meterTime >= 100) {
      this.meterTime = now;
      const level = this.frame.input.rms >= .003 ? Math.min(1, this.frame.input.rms * 5) : 0;
      if (level !== this.view.input.level) this.input({ level });
    }
    return this.frame;
  };

  private pageHide = (event: PageTransitionEvent): void => {
    this.stopMicrophone(); this.pauseOutput();
    if (event.persisted) void this.outputGraph?.context.suspend().catch(() => {});
    else { this.closeOutput(); this.releaseBlob(); }
  };
}

export function useDemoAudio(options: DemoAudioOptions): DemoAudio {
  const latest = useRef(options); latest.current = options;
  const [controller] = useState(() => new DemoAudioController(() => latest.current));
  const [view, setView] = useState<AudioView>(() => controller.view);
  const audioRef = useCallback<RefCallback<HTMLAudioElement>>(element => { controller.attach(element); }, [controller]);
  useEffect(() => controller.mount(setView), [controller]);
  return { audioRef, input: view.input, output: view.output, toggleMicrophone: controller.toggleMicrophone,
    setInputDevice: controller.setInputDevice, playSample: controller.playSample, chooseFile: controller.chooseFile,
    pauseOutput: controller.pauseOutput, stopMicrophone: controller.stopMicrophone, sample: controller.sample };
}
