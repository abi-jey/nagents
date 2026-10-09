export type Mode = "listen" | "think" | "speak" | "delegate" | "connect" | "error";
/** Listening is an independent audio layer and can accompany any activity. */
export type ActivityMode = "idle" | "thinking" | "speaking" | "delegating" | "connecting" | "error";
export type Palette = "mint" | "ice" | "iris";

export interface SignalFrame {
  active: boolean;
  rms: number;
  low: number;
  mid: number;
  high: number;
}

export interface AudioFrame {
  input: SignalFrame;
  output: SignalFrame;
}

export const silentSignal = (): SignalFrame => ({ active: false, rms: 0, low: 0, mid: 0, high: 0 });

export interface SphereConfig {
  mode: Mode;
  playing: boolean;
  density: number;
  speed: number;
  thinkMin: number;
  thinkMax: number;
  glow: number;
  color: Palette;
  dark: boolean;
  reducedMotion: boolean;
}

export interface DensityProfile {
  label: string;
  surface: number;
  core: number;
}

export const densityProfiles: readonly DensityProfile[] = [
  { label: "Sparse", surface: 1, core: 0 },
  { label: "Light", surface: 1.5, core: 0 },
  { label: "Balanced", surface: 2, core: 1 },
  { label: "Detailed", surface: 2.5, core: 1 },
  { label: "Dense", surface: 3, core: 2 },
  { label: "Very dense", surface: 3.25, core: 2 },
  { label: "Maximum", surface: 3.5, core: 2 },
];

export type DelegationOutcome = "failed" | "cancelled";
export type TaskPhase = "launch" | "pending" | "return" | "complete" | DelegationOutcome;

export interface TaskView {
  id: string;
  label: string;
  slot: number;
  phase: TaskPhase;
  resultQueued?: boolean;
}

export interface SphereSnapshot {
  mode: Mode;
  nodes: number;
  edges: number;
  origin: number;
  zoom: number;
  tasks: readonly TaskView[];
  delegationStatus: string;
  inputLevel: number;
  outputLevel: number;
  outerScale: number;
  innerScale: number;
  ambientPackets: number;
  voicePackets: number;
  shock: number;
}

/** Canvas simulation owned by the React lifecycle, with no page-global UI state. */
export interface SphereEngine {
  configure(config: SphereConfig): void;
  tick(seconds: number, audio: AudioFrame): void;
  render(context: CanvasRenderingContext2D, width: number, height: number, compact?: boolean): void;
  snapshot(): SphereSnapshot;
  rotate(yawDelta: number, pitchDelta?: number): void;
  setDragging(dragging: boolean): void;
  pointer(x: number, y: number): void;
  clearPointer(): void;
  selectAt(x: number, y: number): boolean;
  nextNode(): void;
  pulse(node?: number): void;
  zoomBy(delta: number): void;
  resetView(): void;
  /** Rejects displayed IDs and the 128 most recently retired IDs; reset clears this history. */
  startDelegation(id?: string, label?: string): boolean;
  deliverResult(id?: string): boolean;
  finishDelegation(id: string, outcome: DelegationOutcome): boolean;
  resetDelegations(): void;
  dispose(): void;
}
