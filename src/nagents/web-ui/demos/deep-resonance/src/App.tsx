import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useDemoAudio } from "./useDemoAudio";
import { VoiceSphereCanvas } from "./VoiceSphere";
import { useVoiceSphere } from "./useVoiceSphere";
import { densityProfiles, type ActivityMode, type Palette, type TaskPhase } from "./types";
import sampleUrl from "./assets/voice-sample.mp3";

function useMedia(query: string) {
  const [matches, setMatches] = useState(() => typeof matchMedia !== "undefined" && matchMedia(query).matches);
  useEffect(() => {
    const media = matchMedia(query), update = () => setMatches(media.matches);
    update(); media.addEventListener("change", update); return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

function Range({ id, label, value, min, max, step, text, onChange }: { id: string; label: string; value: number; min: number; max: number; step: number; text: string; onChange: (value: number) => void }) {
  return <div className="range"><label htmlFor={id}>{label}</label><output id={`${id}-value`} htmlFor={id}>{text}</output><input id={id} type="range" min={min} max={max} step={step} value={value} aria-valuetext={text} onChange={event => onChange(Number(event.target.value))} /></div>;
}

const modes: readonly { value: ActivityMode; label: string; legacy: string }[] = [
  { value: "idle", label: "Idle / listen", legacy: "listen" }, { value: "thinking", label: "Think", legacy: "think" },
  { value: "speaking", label: "Speak", legacy: "speak" }, { value: "delegating", label: "Delegate", legacy: "delegate" },
];
const phases: Record<TaskPhase, string> = { launch: "Sending", pending: "Working", return: "Result returning", complete: "Result received", failed: "Failed", cancelled: "Cancelled" };
const colors: readonly { value: Palette; label: string; hex: string }[] = [{ value: "mint", label: "Mint", hex: "#9cebd9" }, { value: "ice", label: "Ice blue", hex: "#8dcafa" }, { value: "iris", label: "Iris", hex: "#c2a8ef" }];

export function App() {
  const dark = useMedia("(prefers-color-scheme: dark)"), reducedMotion = useMedia("(prefers-reduced-motion: reduce)");
  const [mode, setMode] = useState<ActivityMode>("idle"), [running, setRunning] = useState(!reducedMotion);
  const [density, setDensity] = useState(3), [speed, setSpeed] = useState(1), [luminance, setLuminance] = useState(.65);
  const [color, setColor] = useState<Palette>("mint"), [thinkMin, setThinkMin] = useState(.45), [thinkMax, setThinkMax] = useState(1.65);
  const file = useRef<HTMLInputElement>(null), sequence = useRef(0);
  const audio = useDemoAudio({ sampleUrl, onPlaybackStart: () => { setMode("speaking"); setRunning(!reducedMotion); }, onMicStart: () => setRunning(!reducedMotion) });
  const source = useMemo(() => ({ sample: audio.sample }), [audio.sample]);
  const sphere = useVoiceSphere({ mode, running, density, speed, thinkingSpeed: [thinkMin, thinkMax], luminance, color, dark, reducedMotion, audio: source });
  const { snapshot } = sphere, mic = audio.input.active, playing = audio.output.playing;
  const title = mode === "idle" ? (mic ? "Listening" : "Idle") : mode === "thinking" ? (mic ? "Thinking + listening" : "Thinking") : mode === "delegating" ? (mic ? "Delegating + listening" : "Delegating") : playing ? (mic ? "Speaking + listening" : "Speaking") : (mic ? "Listening · playback ready" : "Ready for playback");
  const caption = mode === "idle" ? (mic ? "Your voice draws the network inward." : "Ready when you are.") : mode === "thinking" ? (mic ? "Thinking it through while listening." : "Connecting the pieces…") : mode === "delegating" ? (mic ? "Work is underway. I’m still listening." : "Working together on your request.") : playing ? (mic ? "Listening and speaking, together." : "Small movements follow the voice.") : (mic ? "Listening. Ready for playback." : "Play the sample to try speaking.");
  const notes: Record<ActivityMode, string> = { idle: "Signals choose fresh branches at each node. Microphone activity adds incoming signals.", thinking: "Signals explore less-used connections at different paces. Every arrival gives a small recoil.", speaking: "Small, quick expansions track playback. Microphone signals can flow inward at the same time.", delegating: "Tasks and results take varied routes through the core, with impacts at every node." };
  const activeTasks = snapshot.tasks.filter(task => !["complete", "failed", "cancelled"].includes(task.phase));
  const returnable = snapshot.tasks.find(task => !task.resultQueued && (task.phase === "pending" || task.phase === "launch"));
  const startTask = () => sphere.dispatch({ type: "delegation-start", id: `demo-${++sequence.current}` });
  const setActivity = (next: ActivityMode) => { if (next !== "speaking") audio.pauseOutput(); setMode(next); if (next === "delegating") startTask(); };
  const toggleRunning = () => { if (running) { audio.pauseOutput(); audio.stopMicrophone(); } setRunning(!running); };

  const accent = ({ mint: ["#167d65", "#91ebd9"], ice: ["#1c6aa6", "#8dcafa"], iris: ["#774dad", "#c2a8ef"] } as const)[color][dark ? 1 : 0];
  const themeStyle: CSSProperties & { "--accent": string } = { "--accent": accent };
  return <main style={themeStyle} data-react-demo="revision-4" data-mode={mode} data-rendered={snapshot.nodes > 0}>
    <header className="topbar"><div className="brand"><span className="wordmark">ngn</span><span className="brand-separator" /><span className="eyebrow">Audio-reactive studies</span></div><nav aria-label="Deep resonance revisions"><a href="./" aria-current="page">React · revision 4</a><a href="../deep-resonance-v3.html">Revision 3</a></nav></header>
    <div className="heading"><div><h1>Deep resonance · revision 4</h1><p className="subtitle">Quick voice responses, varied signal paths, and listening alongside every state.</p></div><span className="prototype-label">Interactive concept</span></div>
    <div className="voice-io">
      <section className="input-capture" aria-label="Local microphone preview" data-state={audio.input.starting ? "requesting" : mic ? "listening" : "off"}>
        <div className="input-meta"><strong>Microphone input</strong><span role="status">{audio.input.status}</span></div>
        <button id="input-toggle" type="button" aria-pressed={mic || audio.input.starting} onClick={() => void audio.toggleMicrophone()}>{mic || audio.input.starting ? "Stop microphone" : "Start microphone"}</button>
        <div className="input-tools"><label className="input-device-wrap" htmlFor="input-device">Input device<select id="input-device" value={audio.input.deviceId} disabled={audio.input.starting} onChange={event => void audio.setInputDevice(event.target.value)}>{audio.input.devices.map(device => <option key={device.id} value={device.id}>{device.label}</option>)}</select></label><div className="input-meter-wrap"><span>Input level</span><div><meter min={0} max={1} value={audio.input.level} aria-label="Microphone input activity" /><output>{Math.round(audio.input.level * 100)}%</output></div></div></div>
      </section>
      <section className="output-player" aria-label="Speech for the network preview"><div className="output-meta"><strong title={audio.output.fileName}>{audio.output.fileName}</strong><span role="status">{audio.output.status}</span></div><button id="play-sample" className="sample-play" type="button" onClick={() => void audio.playSample()}>Play sample</button><audio ref={audio.audioRef} controls preload="metadata" aria-label="Preview speech playback" /><button type="button" onClick={() => file.current?.click()}>Choose audio</button><input ref={file} type="file" accept="audio/*" aria-label="Choose a local audio file" onChange={event => { const selected = event.target.files?.[0]; if (selected) audio.chooseFile(selected); event.target.value = ""; }} /></section>
    </div>
    <section className="delegation-demo" aria-label="Demo delegation events"><div className="delegation-demo-copy"><strong>Delegation events · demo</strong><span role="status">{snapshot.delegationStatus}</span></div><div className="delegation-demo-actions"><button type="button" disabled={activeTasks.length >= 3} onClick={startTask}>Start delegation</button><button type="button" disabled={!returnable} onClick={() => { if (returnable) sphere.dispatch({ type: "delegation-result", id: returnable.id }); }}>Deliver result</button><button type="button" disabled={!snapshot.tasks.length} onClick={() => sphere.dispatch({ type: "delegations-reset" })}>Reset</button></div><ul className="delegation-demo-tasks" aria-label="Demo task states">{snapshot.tasks.map(task => <li key={task.id} data-phase={task.phase}>{task.label} · {task.resultQueued && task.phase === "launch" ? "Result queued" : phases[task.phase]}</li>)}</ul></section>
    <div className="workspace"><section className="visual" aria-label="Interactive network sphere"><div className="stage"><VoiceSphereCanvas controller={sphere} /><span className="stage-tag"><i /><span role="status">{title}</span></span><span className="stage-counter">{snapshot.nodes} nodes · {snapshot.edges} connections</span><div className="stage-hint"><span>Drag to rotate · click a node to send a pulse</span><span>Scroll to zoom</span></div></div><div className="composer"><VoiceSphereCanvas controller={sphere} compact /><div className="composer-copy"><p className="caption">{caption}</p><span className="voice-status"><i /><span>{title}</span><span>· {mic ? (playing ? "Mic + audio" : "Mic") : "Local demo"}</span></span></div><span className="scale">80 px</span></div></section>
      <aside className="settings" aria-label="Animation and network controls"><div className="modes-panel"><div className="settings-title"><span>Voice animation</span><button className="play" type="button" aria-pressed={running} onClick={toggleRunning}>{running ? "Pause" : "Animate"}</button></div><div className="modes" role="group" aria-label="Voice animation">{modes.map(item => <button key={item.value} type="button" data-mode={item.legacy} aria-pressed={mode === item.value} onClick={() => setActivity(item.value)}>{item.label}</button>)}</div><span className="live-note">{notes[mode]}</span></div>
        <div className="controls"><Range id="density" label="Network density" value={density} min={1} max={7} step={1} text={`${densityProfiles[density - 1].label} · ${snapshot.nodes}`} onChange={setDensity} /><Range id="speed" label="Signal speed" value={speed} min={.2} max={2.4} step={.1} text={`${speed.toFixed(1)}×`} onChange={setSpeed} /><Range id="glow" label="Luminance" value={luminance} min={.1} max={1} step={.01} text={`${Math.round(luminance * 100)}%`} onChange={setLuminance} /></div>
        {mode === "thinking" && <fieldset className="thinking-range"><legend>Thinking signal speeds</legend><Range id="think-min" label="Slowest" value={thinkMin} min={.2} max={2.6} step={.05} text={`${thinkMin.toFixed(2)}×`} onChange={value => { setThinkMin(value); setThinkMax(Math.max(value, thinkMax)); }} /><Range id="think-max" label="Fastest" value={thinkMax} min={.2} max={2.6} step={.05} text={`${thinkMax.toFixed(2)}×`} onChange={value => { setThinkMax(value); setThinkMin(Math.min(value, thinkMin)); }} /><p>Each new signal takes a pace within this range.</p></fieldset>}
        <p className="instructions rotation-note">Gentle rotation in every mode.</p><div className="swatches" role="group" aria-label="Signal color">{colors.map(item => <button key={item.value} style={{ backgroundColor: item.hex }} type="button" aria-label={`${item.label} signals`} aria-pressed={color === item.value} onClick={() => setColor(item.value)} />)}<span>Signal color</span></div><button className="reset" type="button" onClick={() => sphere.dispatch({ type: "view-reset" })}>Reset view</button><p className="instructions">Hover to explore connections.<br />Each pulse follows the network.</p></aside>
    </div>
    <footer className="footer"><div><div className="signal-key"><span><i />Connected nodes</span><span>Responsive network · revision 4</span></div><span className="node-status" role="status">Selected node {snapshot.origin + 1} of {snapshot.nodes}</span></div><div className="view-control" role="group" aria-label="Sphere controls"><button type="button" onClick={() => sphere.rotate(-.22)}>Rotate left</button><button type="button" onClick={() => sphere.rotate(.22)}>Rotate right</button><button type="button" onClick={sphere.nextNode}>Next node</button><button type="button" onClick={() => { setRunning(true); sphere.dispatch({ type: "pulse" }); }}>Send pulse</button><button type="button" disabled={snapshot.zoom <= .65} onClick={() => sphere.zoomBy(-.1)}>Zoom out</button><button type="button" disabled={snapshot.zoom >= 1.25} onClick={() => sphere.zoomBy(.1)}>Zoom in</button></div></footer>
  </main>;
}
