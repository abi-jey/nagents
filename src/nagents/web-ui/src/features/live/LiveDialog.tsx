import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Icon } from "../../components/Icon.js";
import { capability, liveApi } from "./api.js";
import { browserMedia, liveSupport } from "./browser.js";
import { LiveController } from "./controller.js";
import type { LiveCapability, LivePhase, LiveSettingsSnapshot } from "./types.js";
import { LiveSettings, providerLabel } from "./LiveSettings.js";
import "./live.css";

const labels: Record<LivePhase, string> = {
  idle: "Ready to connect", permission: "Waiting for microphone", connecting: "Connecting",
  connected: "Connected", ending: "Ending conversation", ended: "Conversation ended", error: "Disconnected",
};

function duration(seconds: number) {
  return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}

export function LiveDialog({ token, close }: { token: string; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useRef<HTMLHeadingElement>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const loadingRequest = useRef<AbortController | undefined>(undefined);
  const [controller] = useState(() => new LiveController({ ...liveApi(token), media: browserMedia }));
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [config, setConfig] = useState<LiveCapability>();
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [voice, setVoice] = useState("");
  const [now, setNow] = useState(Date.now());
  const [settingsOpen, setSettingsOpen] = useState(false);
  const firstLoad = useRef(true);
  const unsupported = liveSupport();
  const active = ["permission", "connecting", "connected", "ending"].includes(state.phase);
  const connected = state.phase === "connected";
  const elapsed = state.startedAt ? Math.max(0, Math.floor(((state.endedAt || now) - state.startedAt) / 1000)) : 0;
  const occupied = config?.active_session_id && config.active_session_id !== state.sessionId;
  const unavailable = unsupported || (config && !config.available ? config.reason : "") || (occupied ? "Another Live conversation is active. End it in its browser tab, or wait for a closed tab's session to expire, then refresh." : "");
  const status = state.phase === "idle" ? loading ? "Checking availability" : unavailable || loadError ? "Setup needed" : labels.idle : labels[state.phase];
  const provider = providerLabel(config?.provider || "Hosted intelligence");

  async function refresh() {
    loadingRequest.current?.abort();
    const abort = new AbortController(); loadingRequest.current = abort;
    setLoading(true); setLoadError("");
    try {
      const next = await capability(token, AbortSignal.any([abort.signal, AbortSignal.timeout(10_000)]));
      if (!abort.signal.aborted) {
        setConfig(next); setVoice((current) => current || next.voice);
        if (firstLoad.current) { firstLoad.current = false; setSettingsOpen(!next.enabled || !next.key_configured); }
        return next;
      }
    } catch (cause) {
      if (!abort.signal.aborted) setLoadError(cause instanceof Error ? cause.message : "Could not load Live configuration.");
    } finally { if (!abort.signal.aborted) setLoading(false); }
  }

  async function saved(snapshot: LiveSettingsSnapshot) {
    setVoice(snapshot.values.voice);
    const next = await refresh();
    if (next?.available) { setSettingsOpen(false); requestAnimationFrame(() => dialog.current?.querySelector<HTMLButtonElement>(".live-connect")?.focus()); }
  }
  function configure() { setSettingsOpen(true); requestAnimationFrame(() => document.getElementById("live-settings-title")?.focus()); }
  function closeSettings() { setSettingsOpen(false); requestAnimationFrame(() => document.getElementById("live-settings-toggle")?.focus()); }

  useEffect(() => {
    const element = dialog.current, previous = document.activeElement;
    element?.showModal(); title.current?.focus(); void refresh();
    const leave = () => { void controller.end(); };
    window.addEventListener("pagehide", leave);
    return () => {
      window.removeEventListener("pagehide", leave); loadingRequest.current?.abort(); controller.dispose(); element?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, [controller]);
  useEffect(() => {
    if (!connected) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [connected]);
  useEffect(() => {
    if (follow.current && transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
  }, [state.captions]);
  useEffect(() => { if (state.phase === "ended" || state.phase === "error") void refresh(); }, [state.phase]);

  const heading = state.phase === "permission" ? "A little permission first."
    : state.phase === "connecting" ? "Making the connection."
      : connected ? (state.micMuted ? "Take your time." : "You're live.")
        : state.phase === "ending" ? "See you in a moment."
          : state.phase === "ended" ? "Until the next idea."
            : state.phase === "error" ? "Let's try that again."
              : "Think out loud.";
  const subtitle = state.phase === "permission" ? "Allow microphone access in your browser to continue."
    : state.phase === "connecting" ? "Setting up a direct, low-latency audio connection."
      : connected ? (state.micMuted ? "Your microphone is muted. You can still listen." : "Speak naturally. Interrupt, explore, and follow your curiosity.")
        : state.phase === "ended" ? "Your microphone is off. Start fresh whenever you're ready."
          : "A conversation that moves at the speed of your thoughts.";

  return <dialog ref={dialog} className="live-dialog" aria-labelledby="live-title" onCancel={(event) => { event.preventDefault(); close(); }}>
    <header className="live-header">
      <div className="live-brand"><span className="live-brand-icon"><Icon name="wave" size={21} /></span><div>
        <h2 id="live-title" ref={title} tabIndex={-1}>GPT-Live <span>VOICE STUDIO</span></h2>
        <p>A little less typing. A little more presence.</p>
      </div></div>
      <div className="live-header-actions"><button type="button" id="live-settings-toggle" className="live-settings-toggle" disabled={active} aria-label="Connection settings" aria-expanded={settingsOpen} onClick={settingsOpen ? closeSettings : configure}><Icon name="settings" size={17} /><span>Connection settings</span></button><button type="button" className="live-close" aria-label={active ? "End voice and close" : "Close voice studio"} title={active ? "End voice and close" : "Close"} onClick={close}><Icon name="close" size={20} /></button></div>
    </header>
    <div className="live-layout">
      <section className={`live-stage ${connected ? "is-connected" : ""}`} aria-label="Voice connection">
        <div className="live-stage-meta"><span className={`live-status ${connected ? "on" : ""}`} role="status"><i />{status}</span><span className="live-duration" aria-label={`Call duration ${duration(elapsed)}`}>{duration(elapsed)}</span></div>
        <div className={`live-orbit ${active ? "awake" : ""}`} aria-hidden="true"><div className="live-orbit-ring" /><div className="live-orb"><div className="live-orb-shine" /><div className="live-wave">{[0, 1, 2, 3, 4, 5, 6].map((i) => <i key={i} style={{ animationDelay: `${i * -0.17}s` }} />)}</div></div><span className="live-orbit-star one" /><span className="live-orbit-star two" /></div>
        <div className="live-intro"><span className="live-eyebrow">SPACE FOR A REAL CONVERSATION</span><h3>{heading}</h3><p>{subtitle}</p></div>
        <div className="live-actions">
          {active ? <>
            <button type="button" className={`live-control ${state.micMuted ? "muted" : ""}`} aria-label={state.micMuted ? "Unmute microphone" : "Mute microphone"} title={state.micMuted ? "Unmute microphone" : "Mute microphone"} aria-pressed={state.micMuted} disabled={!connected} onClick={() => controller.muteInput()}><Icon name={state.micMuted ? "mic-off" : "mic"} size={21} /></button>
            <button type="button" className="live-end" disabled={state.phase === "ending"} onClick={() => void controller.end()}><Icon name="phone-end" size={20} />{connected ? "End conversation" : state.phase === "ending" ? "Ending…" : "Cancel connection"}</button>
            <button type="button" className={`live-control ${state.outputMuted ? "muted" : ""}`} aria-label={state.outputMuted ? "Unmute speaker" : "Mute speaker"} title={state.outputMuted ? "Unmute speaker" : "Mute speaker"} aria-pressed={state.outputMuted} disabled={!connected} onClick={() => controller.muteOutput()}><Icon name={state.outputMuted ? "volume-off" : "volume"} size={21} /></button>
          </> : <button type="button" className="live-connect" disabled={settingsOpen || loading || !!loadError || !config?.available || !!unavailable} onClick={() => { if (!config) return; follow.current = true; void controller.start(voice, config.revision); }}><Icon name="wave" size={21} />{loading ? "Checking connection…" : state.phase === "idle" ? "Start conversation" : "Start a new conversation"}<span aria-hidden="true">↗</span></button>}
        </div>
        {state.playbackBlocked && <button type="button" className="live-enable-audio" onClick={() => void controller.play()}><Icon name="volume" />Enable audio playback</button>}
        <p className="live-audio-note">{connected ? `${state.micMuted ? "Microphone muted" : "Microphone on"} · ${state.outputMuted ? "Speaker muted" : "Speaker on"}` : settingsOpen ? "Save your settings, then start a conversation." : "Your microphone turns on only when you connect."}</p>
        {(unavailable || loadError || state.error || state.notice) && <div className={`live-feedback ${state.error || loadError ? "has-error" : ""}`} role={state.error || loadError ? "alert" : "status"}>
          {(state.error || loadError) && <p>{state.error || loadError}</p>}
          {unavailable && <p>{unavailable}</p>}
          {state.notice && <p>{state.notice}</p>}
          {!active && <div className="live-feedback-actions"><button type="button" disabled={loading} onClick={configure}>Configure connection</button><button type="button" disabled={loading} onClick={() => void refresh()}>Refresh</button></div>}
        </div>}
        <div className="live-configuration"><label htmlFor="live-voice">VOICE<select id="live-voice" value={voice} disabled={active || settingsOpen || !config || loading} onChange={(event) => setVoice(event.target.value)}>{[...new Set([config?.voice || "marin", ...(config?.voices || [])])].map((name) => <option key={name} value={name}>{name.charAt(0).toUpperCase() + name.slice(1)}</option>)}</select></label><div><span>VOICE MODEL</span><strong>{config?.model || "gpt-live-1"}</strong></div><div><span>BACKEND</span><strong title={config?.backend_model}>{config?.backend_model || "Set in connection settings"}</strong></div></div>
      </section>
      {settingsOpen ? <LiveSettings token={token} blocked={active || !!config?.active_session_id} saved={(snapshot) => void saved(snapshot)} back={closeSettings} /> : <section className="live-transcript-panel" aria-label="Live captions">
        <header><div><Icon name="chat" size={17} /><h3>The conversation</h3></div><span className="live-caption-label">LIVE CAPTIONS</span></header>
        <div className="live-transcript" ref={transcript} role="log" aria-label="Conversation captions" aria-live="polite" aria-relevant="additions text" tabIndex={0} onScroll={(event) => { const el = event.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 70; }}>
          {state.captions.length ? state.captions.map((caption) => <article key={caption.id} className={`live-caption ${caption.speaker}`}><div className="live-caption-meta"><span className="live-speaker-mark">{caption.speaker === "user" ? "Y" : <Icon name="wave" size={13} />}</span><strong>{caption.speaker === "user" ? "You" : "GPT-Live"}</strong><time>{duration(Math.floor(caption.start / 1000))}</time></div><p>{caption.text}</p></article>) : <div className="live-transcript-empty"><span><Icon name="chat" size={28} /></span><h4>Words will find their way here.</h4><p>Your conversation appears as you speak.<br />For now, just bring yourself.</p><div className="live-caption-preview" aria-hidden="true"><i /><i /><i /></div></div>}
        </div>
        <footer><span className="live-privacy-dot" /><p>Captions stay in this view. This voice conversation is separate from your workspace chat.</p></footer>
      </section>}
    </div>
    <footer className="live-footer"><span><i />POWERED BY GPT-LIVE</span><p>{provider} · Browser audio · No workspace tools</p></footer>
  </dialog>;
}
