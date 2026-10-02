import type { ReactNode } from "react";
import { Icon } from "../../components/Icon.js";
import type { LiveState } from "./types.js";

export interface LiveDockProps {
  state: LiveState;
  status: string;
  elapsed: string;
  subtitle: string;
  disabled: boolean;
  unavailable: string;
  loading: boolean;
  voice: string;
  provider: string;
  backend: string;
  settingsOpen: boolean;
  settingsSaving: boolean;
  start(): void;
  end(): void;
  close(): void;
  configure(): void;
  muteInput(): void;
  muteOutput(): void;
  play(): void;
  refresh(): void;
  children?: ReactNode;
}

export function LiveDock({ state, status, elapsed, subtitle, disabled, unavailable, loading, voice, provider, backend, settingsOpen, settingsSaving, start, end, close, configure, muteInput, muteOutput, play, refresh, children }: LiveDockProps) {
  const connected = state.phase === "connected";
  const active = ["permission", "connecting", "connected", "ending"].includes(state.phase);
  const heading = connected ? state.micMuted ? "Take your time." : "You're live."
    : state.phase === "permission" ? "Allow your microphone."
      : state.phase === "connecting" ? "Connecting…"
        : state.phase === "ending" ? "Finishing up…"
          : state.phase === "error" ? "Let's reconnect."
            : state.phase === "ended" ? "Until your next idea."
              : "Think out loud.";
  const feedback = state.error || unavailable || state.notice;
  const voiceLabel = voice ? voice.charAt(0).toUpperCase() + voice.slice(1) : "Your voice";

  return <aside id="live-voice-dock" className={`live-dock phase-${state.phase}${active ? " is-active" : ""}${connected ? " is-connected" : ""}${settingsOpen ? " settings-open" : ""}`} aria-label="Live voice">
    <div className="live-dock-surface">
      <header className="live-dock-header">
        <div className="live-dock-brand"><span aria-hidden="true"><Icon name="wave" size={15} /></span><h2 id="live-dock-title" tabIndex={-1}>GPT-Live</h2></div>
        <div className="live-dock-header-actions">
          <button type="button" aria-label="Voice settings" title="Voice settings" aria-expanded={settingsOpen} aria-controls="live-device-popover" disabled={active || settingsSaving} onClick={configure}><Icon name="settings" size={17} /></button>
          <button type="button" aria-label={active ? "End voice and close" : "Close Live voice"} title={active ? "End voice and close" : "Close Live voice"} disabled={settingsSaving} onClick={close}><Icon name="close" size={18} /></button>
        </div>
      </header>
      <div className="live-dock-body">
        <div className="live-dock-avatar" aria-hidden="true"><div className="live-dock-orbit" /><div className="live-dock-orb"><div className="live-dock-orb-color" /><div className="live-dock-orb-light" /><div className="live-dock-orb-core"><i /><i /><i /><i /><i /></div></div><div className="live-dock-orb-shadow" /><i className="live-dock-glint" /></div>
        <div className="live-dock-copy">
          <div className="live-dock-state"><span role="status"><i aria-hidden="true" />{status}</span><time aria-label={`Voice duration ${elapsed}`}>{elapsed}</time></div>
          <h3>{heading}</h3>
          <p className="live-dock-subtitle">{subtitle}</p>
          <div className="live-dock-identity" title={`${provider} · ${backend}`}><span>{voiceLabel}</span><i aria-hidden="true" />Your assistant</div>
        </div>
      </div>
      <div className="live-dock-controls" aria-label="Voice controls">
        {active ? <>
          <button type="button" className={`live-dock-control${state.micMuted ? " is-muted" : ""}`} aria-label={state.micMuted ? "Unmute microphone" : "Mute microphone"} title={state.micMuted ? "Unmute microphone" : "Mute microphone"} aria-pressed={state.micMuted} disabled={!connected} onClick={muteInput}><Icon name={state.micMuted ? "mic-off" : "mic"} size={19} /></button>
          <button type="button" className="live-dock-end" aria-label={connected ? "End voice" : state.phase === "ending" ? "Ending voice" : "Cancel connection"} disabled={state.phase === "ending"} onClick={end}><Icon name="phone-end" size={18} /><span>{connected ? "End voice" : state.phase === "ending" ? "Ending…" : "Cancel"}</span></button>
          <button type="button" className={`live-dock-control${state.outputMuted ? " is-muted" : ""}`} aria-label={state.outputMuted ? "Unmute speaker" : "Mute speaker"} title={state.outputMuted ? "Unmute speaker" : "Mute speaker"} aria-pressed={state.outputMuted} disabled={!connected} onClick={muteOutput}><Icon name={state.outputMuted ? "volume-off" : "volume"} size={19} /></button>
        </> : <button type="button" className="live-dock-start" disabled={disabled || settingsSaving || settingsOpen || loading} onClick={start}><Icon name="wave" size={18} /><span>{loading ? "Checking…" : state.phase === "error" ? "Reconnect" : state.phase === "ended" ? "Start again" : "Start voice"}</span><span className="live-dock-start-arrow" aria-hidden="true">↗</span></button>}
      </div>
      {connected && <p className="live-dock-audio-status" role="status">{state.micMuted ? "Microphone muted" : "Microphone on"} · {state.outputMuted ? "Speaker muted" : "Speaker on"}</p>}
      {state.playbackBlocked && <button type="button" className="live-dock-play" onClick={play}><Icon name="volume" size={16} />Enable audio</button>}
      {feedback && <div className={`live-dock-feedback${state.error ? " has-error" : ""}`} role={state.error ? "alert" : "status"}><p>{feedback}</p>{!active && <div><button type="button" disabled={loading || settingsSaving} onClick={configure}>Voice settings</button><button type="button" disabled={loading} onClick={refresh}>Refresh</button></div>}</div>}
      <footer className="live-dock-footer"><Icon name="chat" size={12} /><span>Captions appear in this chat</span></footer>
    </div>
    <div className="live-dock-settings" id="live-device-popover" hidden={!settingsOpen}>{children}</div>
  </aside>;
}
