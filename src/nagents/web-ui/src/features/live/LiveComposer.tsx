import type { ReactNode } from "react";
import { Icon } from "../../components/Icon.js";
import { currentLiveDelegation } from "./controller.js";
import { LiveSphere } from "./LiveSphere.js";
import { contextLabel } from "./context.js";
import type { LiveState } from "./types.js";

export interface LiveComposerProps {
  state: LiveState;
  status: string;
  elapsed: string;
  disabled: boolean;
  unavailable: string;
  loading: boolean;
  voice: string;
  settingsOpen: boolean;
  settingsSaving: boolean;
  inspectionOpen: boolean;
  start(): void;
  end(): void;
  close(): void;
  configure(): void;
  muteInput(): void;
  muteOutput(): void;
  play(): void;
  refresh(): void;
  viewChat(runId: string): void;
  inspect(): void;
  inspector?: ReactNode;
  contextOpen?: boolean;
  showContext?(): void;
  contextDetails?: ReactNode;
  audioNotice?: ReactNode;
}

/** The voice presence and controls share the composer's layout, never cover chat. */
export function LiveComposer({ state, status, elapsed, disabled, unavailable, loading, voice, settingsOpen, settingsSaving, inspectionOpen, start, end, close, configure, muteInput, muteOutput, play, refresh, viewChat, inspect, inspector, contextOpen = false, showContext, contextDetails, audioNotice }: LiveComposerProps) {
  const connected = state.phase === "connected";
  const active = ["permission", "connecting", "connected", "ending"].includes(state.phase);
  const delegation = currentLiveDelegation(state.delegations);
  const unfinished = delegation?.status === "queued" || delegation?.status === "working";
  const pending = state.delegations.filter(item => item.status === "queued" || item.status === "working").length;
  const delegationLabel = delegation ? !connected && unfinished ? "Assistant still working in chat" : {
    queued: "Request queued", working: "Assistant working", completed: "Request complete", failed: "Request failed", cancelled: "Request stopped",
  }[delegation.status] : "";
  const routineEnd = state.phase === "ended" && ["Live session closed. Finalization confirmed.", "Conversation ended. Your microphone is off."].includes(state.notice);
  const feedback = state.error || unavailable || (routineEnd ? "" : state.notice);
  const voiceLabel = voice ? voice.charAt(0).toUpperCase() + voice.slice(1) : "Voice";
  const startDisabled = disabled || settingsSaving || settingsOpen || loading;
  const interactionDisabled = connected ? false : active || startDisabled;

  return <section id="voice-session" className={`voice-composer phase-${state.phase}${connected ? " is-connected" : ""}${inspectionOpen || contextOpen ? " has-expansion" : ""}`} aria-label="Voice in this chat" onKeyDown={event => {
    if (event.key === "Escape" && contextOpen) { event.stopPropagation(); showContext?.(); }
    else if (event.key === "Escape" && inspectionOpen) { event.stopPropagation(); inspect(); }
  }}>
    <div className="voice-presence">
      <LiveSphere phase={state.phase} micMuted={state.micMuted} outputMuted={state.outputMuted}
        busy={!!unfinished && connected} disabled={interactionDisabled}
        inputLevel={state.inputLevel || 0} outputLevel={state.outputLevel || 0}
        onActivate={connected ? muteInput : start} />
    </div>
    <div className="voice-composer-bar">
      <div className="voice-composer-status">
        <span role="status"><i aria-hidden="true" />{connected ? state.micMuted ? "Mic muted" : "Voice is on" : status}</span>
        <small>{voiceLabel}<span aria-hidden="true"> · </span>{state.startedAt > 0 ? <time aria-label={`Voice duration ${elapsed}`}>{elapsed}</time> : "Captions in chat"}{state.context && showContext && <button type="button" className="voice-context-trigger" aria-expanded={contextOpen} aria-controls="voice-inline-context" disabled={settingsSaving} onClick={showContext}>{contextLabel(state.context)}</button>}</small>
      </div>
      <div className="voice-composer-actions" aria-label="Voice controls">
        {active ? <>
          <button type="button" className={`voice-mic${state.micMuted ? " is-muted" : ""}`} aria-label={state.micMuted ? "Unmute microphone" : "Mute microphone"} title={state.micMuted ? "Unmute microphone" : "Mute microphone"} aria-pressed={state.micMuted} disabled={!connected} onClick={muteInput}><Icon name={state.micMuted ? "mic-off" : "mic"} size={18} /></button>
          <button type="button" className={`voice-speaker${state.outputMuted ? " is-muted" : ""}`} aria-label={state.outputMuted ? "Unmute speaker" : "Mute speaker"} title={state.outputMuted ? "Unmute speaker" : "Mute speaker"} aria-pressed={state.outputMuted} disabled={!connected} onClick={muteOutput}><Icon name={state.outputMuted ? "volume-off" : "volume"} size={18} /></button>
        </> : <button type="button" className="voice-start" disabled={startDisabled} onClick={start}><Icon name="wave" size={16} /><span>{state.phase === "error" ? "Retry" : state.phase === "ended" ? "Start again" : "Start voice"}</span></button>}
        <button type="button" className="voice-settings-trigger" aria-label="Audio devices and voice settings" aria-haspopup="dialog" aria-expanded={settingsOpen} aria-controls="voice-settings-dialog" disabled={settingsSaving} onClick={configure}><Icon name="settings" size={16} /><span>Audio</span></button>
        {active ? <button type="button" className="voice-stop" aria-label={connected ? "End voice" : state.phase === "ending" ? "Ending voice" : "Cancel connection"} disabled={state.phase === "ending"} onClick={end}><Icon name="stop" size={15} /><span>{connected ? "Stop" : state.phase === "ending" ? "Ending" : "Cancel"}</span></button> : <button type="button" className="voice-dismiss" aria-label="Close voice controls" title="Close voice controls" disabled={settingsSaving} onClick={close}><Icon name="close" size={17} /></button>}
      </div>
    </div>
    {delegation && <div className="voice-delegation" data-status={delegation.status} aria-label="Voice delegation">
      <code className="voice-event-type">delegation</code><span role="status">{delegationLabel}{connected && pending > 1 ? ` · ${pending} requests` : ""}</span>
      <button type="button" className="voice-request-trigger" aria-expanded={inspectionOpen} aria-controls="voice-inline-delegation" onClick={inspect}>Request &amp; events<Icon name="chevron" size={12} /></button>
      {delegation.runId && <button type="button" className="voice-view-chat" aria-label={`View ${delegation.agent}'s request in chat`} onClick={() => viewChat(delegation.runId)}>View in chat</button>}
    </div>}
    {audioNotice}
    {state.playbackBlocked && <button type="button" className="voice-enable-audio" onClick={play}><Icon name="volume" size={16} />Enable audio</button>}
    {feedback && <div className={`voice-feedback${state.error ? " has-error" : ""}`} role={state.error ? "alert" : "status"}><p>{feedback}</p>{!active && <button type="button" disabled={loading} onClick={refresh}>Refresh</button>}</div>}
    <div className="voice-inline-expansion" id="voice-inline-delegation" hidden={!inspectionOpen}>{inspector}</div>
    <div className="voice-inline-expansion" id="voice-inline-context" hidden={!contextOpen}>{contextDetails}</div>
  </section>;
}
