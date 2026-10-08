import { useCallback, useEffect, useRef, useState } from "react";
import { createSphereEngine } from "./engine.js";
import { silentSignal, type ActivityMode, type AudioFrame, type DelegationOutcome, type Mode, type Palette, type SignalFrame, type SphereConfig, type SphereEngine, type SphereSnapshot } from "./types.js";

export interface AudioSource { sample(): AudioFrame }

export interface VoiceSphereOptions {
  mode?: ActivityMode;
  audio?: AudioSource;
  running?: boolean;
  density?: number;
  speed?: number;
  thinkingSpeed?: readonly [number, number];
  luminance?: number;
  color?: Palette;
  dark?: boolean;
  reducedMotion?: boolean;
  onSnapshot?: (snapshot: SphereSnapshot) => void;
}

export type SphereEvent =
  | { type: "pulse"; node?: number }
  | { type: "delegation-start"; id: string; label?: string }
  | { type: "delegation-result"; id: string }
  | { type: "delegation-finished"; id: string; outcome: DelegationOutcome }
  | { type: "delegations-reset" }
  | { type: "view-reset" };

export interface VoiceSphereHandle {
  dispatch(event: SphereEvent): boolean;
  rotate(yaw: number, pitch?: number): void;
  nextNode(): void;
  zoomBy(delta: number): void;
  getSnapshot(): SphereSnapshot;
}

export interface VoiceSphereController extends VoiceSphereHandle {
  snapshot: SphereSnapshot;
  attach(canvas: HTMLCanvasElement, compact: boolean, ready?: (available: boolean) => void): () => void;
  pointer(x: number, y: number): void;
  clearPointer(): void;
  setDragging(dragging: boolean): void;
  selectAt(x: number, y: number): void;
}

interface Surface {
  context: CanvasRenderingContext2D;
  width: number;
  height: number;
  compact: boolean;
}

export const initialSnapshot: SphereSnapshot = {
  nodes: 0, edges: 0, origin: 0, zoom: 1, tasks: [],
  delegationStatus: "Send a task through the network, then return its result.",
  inputLevel: 0, outputLevel: 0, outerScale: 1, innerScale: 1,
  ambientPackets: 0, voicePackets: 0, shock: 0,
};

const modes: Record<ActivityMode, Mode> = { idle: "listen", thinking: "think", speaking: "speak", delegating: "delegate" };
const finite = (value: number, fallback: number, min: number, max: number) => Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : fallback;

export function normalizeSignal(frame: SignalFrame): SignalFrame {
  if (!frame.active) return silentSignal();
  return { active: true, rms: finite(frame.rms, 0, 0, 1), low: finite(frame.low, 0, 0, 1), mid: finite(frame.mid, 0, 0, 1), high: finite(frame.high, 0, 0, 1) };
}

function configuration(options: VoiceSphereOptions): SphereConfig {
  const range = options.thinkingSpeed ?? [.45, 1.65];
  const low = finite(range[0], .45, .2, 2.6), high = finite(range[1], 1.65, .2, 2.6);
  return {
    mode: modes[options.mode ?? "idle"], playing: options.running ?? true,
    density: Math.round(finite(options.density ?? 3, 3, 1, 7)),
    speed: finite(options.speed ?? 1, 1, .2, 2.4), thinkMin: Math.min(low, high), thinkMax: Math.max(low, high),
    glow: finite(options.luminance ?? .65, .65, .1, 1), color: options.color ?? "mint",
    dark: options.dark ?? false, reducedMotion: options.reducedMotion ?? false,
  };
}

/** Owns simulation and rendering only. Audio capture and playback belong to the host. */
export function useVoiceSphere(options: VoiceSphereOptions = {}): VoiceSphereController {
  const latest = useRef(options); latest.current = options;
  const engine = useRef<SphereEngine | undefined>(undefined);
  const surfaces = useRef(new Map<HTMLCanvasElement, Surface>());
  const [snapshot, setSnapshot] = useState<SphereSnapshot>(initialSnapshot);
  const lastSnapshot = useRef(initialSnapshot);
  const publish = useCallback(() => {
    if (!engine.current) return;
    const next = engine.current.snapshot(); lastSnapshot.current = next; setSnapshot(next);
    latest.current.onSnapshot?.(next);
  }, []);

  useEffect(() => {
    const instance = createSphereEngine(configuration(latest.current)); engine.current = instance;
    let frame = 0, last = 0, published = -Infinity, disposed = false;
    const tick = (now: number) => {
      if (disposed) return;
      frame = requestAnimationFrame(tick);
      if (document.hidden) { last = 0; return; }
      if (last && now - last < 15) return;
      const delta = last ? Math.min(.05, (now - last) / 1000) : 0; last = now;
      let audio: AudioFrame = { input: silentSignal(), output: silentSignal() };
      try {
        const read = latest.current.audio?.sample();
        if (read) audio = { input: normalizeSignal(read.input), output: normalizeSignal(read.output) };
      } catch { /* Optional visualization must survive a host audio adapter failure. */ }
      instance.tick(delta, audio);
      for (const surface of surfaces.current.values()) if (surface.width && surface.height) instance.render(surface.context, surface.width, surface.height, surface.compact);
      if (now - published >= 100) { publish(); published = now; }
    };
    const visible = () => { last = 0; };
    document.addEventListener("visibilitychange", visible);
    publish(); frame = requestAnimationFrame(tick);
    return () => {
      disposed = true; cancelAnimationFrame(frame); document.removeEventListener("visibilitychange", visible);
      instance.dispose(); if (engine.current === instance) engine.current = undefined;
    };
  }, [publish]);

  useEffect(() => { engine.current?.configure(configuration(options)); }, [options.mode, options.running, options.density, options.speed, options.thinkingSpeed?.[0], options.thinkingSpeed?.[1], options.luminance, options.color, options.dark, options.reducedMotion]);

  const attach = useCallback((canvas: HTMLCanvasElement, compact: boolean, ready?: (available: boolean) => void) => {
    let context: CanvasRenderingContext2D | null;
    try { context = canvas.getContext("2d", { alpha: compact }); } catch { context = null; }
    if (!context) { ready?.(false); return () => {}; }
    const painter = context;
    const view = canvas.ownerDocument.defaultView;
    const surface: Surface = { context, width: 0, height: 0, compact };
    const resize = () => {
      surface.width = canvas.clientWidth || (compact ? 80 : 0); surface.height = canvas.clientHeight || (compact ? 80 : 0);
      const scale = Math.min(view?.devicePixelRatio || 1, 2);
      canvas.width = Math.round(surface.width * scale); canvas.height = Math.round(surface.height * scale);
      painter.setTransform(scale, 0, 0, scale, 0, 0);
    };
    surfaces.current.set(canvas, surface); resize(); ready?.(true);
    const Observer = view?.ResizeObserver ?? globalThis.ResizeObserver;
    const observer = Observer ? new Observer(resize) : undefined;
    observer?.observe(canvas);
    if (!observer) view?.addEventListener("resize", resize);
    return () => { observer?.disconnect(); view?.removeEventListener("resize", resize); surfaces.current.delete(canvas); ready?.(false); };
  }, []);

  const dispatch = useCallback((event: SphereEvent) => {
    const current = engine.current; if (!current) return false;
    if ((event.type === "delegation-start" || event.type === "delegation-result" || event.type === "delegation-finished") && (typeof event.id !== "string" || !event.id.trim())) return false;
    if (event.type === "pulse" && event.node !== undefined && (!Number.isInteger(event.node) || event.node < 0 || event.node >= current.snapshot().nodes)) return false;
    let accepted = true;
    switch (event.type) {
      case "pulse": current.pulse(event.node); break;
      case "delegation-start": accepted = current.startDelegation(event.id, event.label); break;
      case "delegation-result": accepted = current.deliverResult(event.id); break;
      case "delegation-finished": accepted = current.finishDelegation(event.id, event.outcome); break;
      case "delegations-reset": current.resetDelegations(); break;
      case "view-reset": current.resetView(); break;
      default: return false;
    }
    publish(); return accepted;
  }, [publish]);
  const rotate = useCallback((yaw: number, pitch = 0) => { engine.current?.rotate(yaw, pitch); }, []);
  const nextNode = useCallback(() => { engine.current?.nextNode(); publish(); }, [publish]);
  const zoomBy = useCallback((delta: number) => { engine.current?.zoomBy(delta); publish(); }, [publish]);
  const getSnapshot = useCallback(() => engine.current?.snapshot() ?? lastSnapshot.current, []);
  const pointer = useCallback((x: number, y: number) => { engine.current?.pointer(x, y); }, []);
  const clearPointer = useCallback(() => { engine.current?.clearPointer(); }, []);
  const setDragging = useCallback((dragging: boolean) => { engine.current?.setDragging(dragging); }, []);
  const selectAt = useCallback((x: number, y: number) => { engine.current?.selectAt(x, y); publish(); }, [publish]);
  return { snapshot, attach, dispatch, rotate, nextNode, zoomBy, getSnapshot, pointer, clearPointer, setDragging, selectAt };
}
