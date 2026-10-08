import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { VoiceSphereCanvas, useVoiceSphere } from "../../components/voiceSphere/index.js";
import { silentSignal, type ActivityMode, type SphereSnapshot } from "../../components/voiceSphere/types.js";
import type { AudioSource, SphereEvent } from "../../components/voiceSphere/useVoiceSphere.js";
import { createLiveSphereEvents } from "./liveSphereEvents.js";
import type { LiveDelegation, LivePhase } from "./types.js";

export interface LiveSphereProps {
  phase: LivePhase;
  micMuted: boolean;
  outputMuted: boolean;
  busy: boolean;
  disabled: boolean;
  inputLevel: number;
  outputLevel: number;
  audio?: AudioSource;
  sessionId?: string;
  delegations?: readonly LiveDelegation[];
  onActivate(): void;
}

const noDelegations: readonly LiveDelegation[] = [];
const level = (value: number): number => Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => typeof window !== "undefined" && Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches));
  useEffect(() => {
    const query = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!query) return;
    const changed = () => setReduced(query.matches);
    changed(); query.addEventListener?.("change", changed);
    return () => query.removeEventListener?.("change", changed);
  }, []);
  return reduced;
}

function NetworkPresence({ phase, micMuted, outputMuted, busy, outputLevel, audio, sessionId = "", delegations = noDelegations, onReady }: LiveSphereProps & { onReady(available: boolean): void }) {
  const reducedMotion = useReducedMotion();
  const bridge = useMemo(() => createLiveSphereEvents(), []);
  const latest = useRef({ phase, micMuted, outputMuted, audio });
  latest.current = { phase, micMuted, outputMuted, audio };
  const actualOutput = useRef(false);
  const [speaking, setSpeaking] = useState(false);
  const dispatch = useRef<((event: SphereEvent) => boolean) | undefined>(undefined);
  const source = useMemo<AudioSource>(() => ({
    sample() {
      const current = latest.current;
      if (current.phase !== "connected") {
        actualOutput.current = false;
        return { input: silentSignal(), output: silentSignal() };
      }
      const frame = current.audio?.sample();
      const input = !current.micMuted && frame ? frame.input : silentSignal();
      const output = !current.outputMuted && frame ? frame.output : silentSignal();
      actualOutput.current = output.active && Number.isFinite(output.rms) && output.rms > .003;
      return { input, output };
    },
  }), []);
  const onSnapshot = useCallback((_snapshot: SphereSnapshot) => {
    setSpeaking(value => value === actualOutput.current ? value : actualOutput.current);
    if (dispatch.current) bridge.flush(dispatch.current);
  }, [bridge]);
  const mode: ActivityMode = phase === "connected" && !outputMuted && (speaking || !audio && level(outputLevel) > .015)
    ? "speaking" : busy ? "delegating" : phase === "permission" || phase === "connecting" ? "thinking" : "idle";
  const controller = useVoiceSphere({ mode, audio: source, density: 3, dark: true, reducedMotion, onSnapshot });
  dispatch.current = controller.dispatch;
  useEffect(() => {
    dispatch.current = controller.dispatch;
    bridge.reset();
    return () => { bridge.reset(); dispatch.current = undefined; };
  }, [bridge, controller.dispatch]);
  const version = delegations.map(record => `${record.id}:${record.seq}:${record.status}`).join("|");
  useEffect(() => { bridge.reconcile(sessionId, delegations, controller.dispatch); }, [bridge, controller.dispatch, sessionId, delegations, version]);
  return <VoiceSphereCanvas controller={controller} compact className="live-sphere-canvas" onReady={onReady} label="Connected voice network" />;
}

/** The network is decorative; the existing native button owns voice activation. */
export function LiveSphere(props: LiveSphereProps) {
  const { phase, micMuted, outputMuted, busy, disabled, inputLevel, outputLevel, onActivate } = props;
  const [rendered, setRendered] = useState(false);
  const capable = typeof window !== "undefined" && typeof window.CanvasRenderingContext2D !== "undefined"
    && typeof requestAnimationFrame === "function" && typeof cancelAnimationFrame === "function";
  const connected = phase === "connected";
  const pending = phase === "permission" || phase === "connecting" || phase === "ending";
  const blocked = disabled || pending;
  const label = connected ? micMuted ? "Unmute voice microphone" : "Mute voice microphone"
    : phase === "permission" ? "Waiting for microphone permission"
      : phase === "connecting" ? "Connecting voice"
        : phase === "ending" ? "Ending voice"
          : phase === "error" ? "Reconnect voice"
            : phase === "ended" ? "Start voice again" : "Start voice";
  const style: CSSProperties & Record<"--voice-input" | "--voice-output", number> = {
    "--voice-input": connected && !micMuted ? level(inputLevel) : 0,
    "--voice-output": connected && !outputMuted ? level(outputLevel) : 0,
  };
  return <button type="button" className="live-sphere" style={style}
    data-phase={phase} data-mic-muted={micMuted} data-output-muted={outputMuted} data-busy={busy} data-renderer={rendered ? "network" : "fallback"}
    aria-label={label} aria-pressed={connected ? micMuted : undefined} aria-busy={pending || undefined}
    title={label} disabled={blocked} onClick={onActivate}>
    <span className="live-sphere-scene" aria-hidden="true">
      <svg className="live-sphere-fallback" viewBox="0 0 80 80" fill="none" aria-hidden="true">
        <g className="live-sphere-links" stroke="currentColor" strokeWidth=".7">
          <path d="m40 9 21 8 10 23-10 23-21 8-21-8L9 40l10-23 21-8Zm0 0L26 25 9 40l17 15 14 16 14-16 17-15-17-15L40 9Zm-21 8 7 38 35 8-7-38-35-8Zm42 0L26 25l-7 38 35-8 7-38ZM26 25l28 30M54 25 26 55M9 40h62M40 9v62" />
          <path className="live-sphere-core" d="m40 26 13 8v14l-13 8-13-8V34l13-8Zm0 0v30M27 34l26 14m0-14L27 48M26 25l14 15 14-15M26 55l14-15 14 15" />
        </g>
        <g fill="currentColor">{[[40, 9], [61, 17], [71, 40], [61, 63], [40, 71], [19, 63], [9, 40], [19, 17], [26, 25], [54, 25], [26, 55], [54, 55]].map(([x, y]) => <circle key={`${x}:${y}`} cx={x} cy={y} r="1.3" />)}</g>
        <g className="live-sphere-core" fill="currentColor">{[[40, 26], [53, 34], [53, 48], [40, 56], [27, 48], [27, 34], [40, 40]].map(([x, y]) => <circle key={`${x}:${y}`} cx={x} cy={y} r="1.5" />)}</g>
      </svg>
      {capable && <NetworkPresence {...props} onReady={setRendered} />}
    </span>
  </button>;
}
