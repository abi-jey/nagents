import { useEffect, useRef, useState, type CSSProperties, type PointerEvent } from "react";
import type { LivePhase } from "./types.js";
import { mountSphereRenderer, type SphereScene } from "./sphereRenderer.js";

export interface LiveSphereProps {
  phase: LivePhase;
  micMuted: boolean;
  outputMuted: boolean;
  busy: boolean;
  disabled: boolean;
  inputLevel: number;
  outputLevel: number;
  onActivate(): void;
}

function level(value: number): number { return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0; }

export function LiveSphere({ phase, micMuted, outputMuted, busy, disabled, inputLevel, outputLevel, onActivate }: LiveSphereProps) {
  const button = useRef<HTMLButtonElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const renderer = useRef<ReturnType<typeof mountSphereRenderer> | undefined>(undefined);
  const pointer = useRef({ x: 0, y: 0, hover: false, pressed: false });
  const [rendered, setRendered] = useState(false);
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
  const scene = useRef<SphereScene>({ phase, muted: micMuted, busy, disabled: blocked, input: 0, output: 0, ...pointer.current });
  scene.current = { phase, muted: micMuted, busy, disabled: blocked, input: style["--voice-input"], output: style["--voice-output"], ...pointer.current };

  function paintPointer() {
    button.current?.style.setProperty("--voice-pointer-x", pointer.current.x.toFixed(3));
    button.current?.style.setProperty("--voice-pointer-y", pointer.current.y.toFixed(3));
    button.current?.style.setProperty("--voice-pressed", pointer.current.pressed ? "1" : "0");
    Object.assign(scene.current, pointer.current);
    renderer.current?.refresh();
  }
  function follow(event: PointerEvent<HTMLButtonElement>) {
    if (blocked) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    pointer.current = {
      ...pointer.current, hover: true,
      x: Math.max(-1, Math.min(1, (event.clientX - bounds.left) / bounds.width * 2 - 1)),
      y: Math.max(-1, Math.min(1, (event.clientY - bounds.top) / bounds.height * 2 - 1)),
    };
    paintPointer();
  }
  function reset() {
    pointer.current = { x: 0, y: 0, hover: false, pressed: false };
    paintPointer();
  }

  useEffect(() => { if (blocked) reset(); }, [blocked]);
  useEffect(() => {
    if (!canvas.current) return;
    const current = mountSphereRenderer(canvas.current, () => scene.current, setRendered);
    renderer.current = current;
    return () => { current.dispose(); renderer.current = undefined; };
  }, []);
  useEffect(() => { renderer.current?.refresh(); }, [phase, micMuted, outputMuted, busy, blocked, inputLevel, outputLevel]);

  return <button ref={button} type="button" className="live-sphere" style={style}
    data-phase={phase} data-mic-muted={micMuted} data-output-muted={outputMuted} data-busy={busy} data-renderer={rendered ? "webgl" : "fallback"}
    aria-label={label} aria-pressed={connected ? micMuted : undefined} aria-busy={pending || undefined}
    title={label} disabled={blocked} onClick={onActivate}
    onPointerEnter={follow} onPointerMove={follow} onPointerLeave={reset} onPointerCancel={reset}
    onPointerDown={(event) => { if (!blocked) { pointer.current.pressed = true; follow(event); } }}
    onPointerUp={(event) => { pointer.current.pressed = false; if (event.pointerType !== "mouse") reset(); else paintPointer(); }}
    onKeyDown={(event) => { if (event.key === " " || event.key === "Enter") { pointer.current.pressed = true; paintPointer(); } }}
    onKeyUp={(event) => { if (event.key === " " || event.key === "Enter") { pointer.current.pressed = false; paintPointer(); } }}
    onBlur={reset}>
    <span className="live-sphere-scene" aria-hidden="true">
      <span className="live-sphere-shadow" />
      <span className="live-sphere-fallback" />
      <canvas ref={canvas} className="live-sphere-canvas" />
    </span>
  </button>;
}
