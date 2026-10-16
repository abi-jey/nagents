import { useLayoutEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../../components/Icon.js";
import { currentLiveDelegation } from "./controller.js";
import { LiveSphere } from "./LiveSphere.js";
import { contextLabel } from "./context.js";
import { VoiceControls } from "./VoiceControls.js";
import type { Caption, LiveState } from "./types.js";
import type { AudioSource } from "../../components/voiceSphere/useVoiceSphere.js";

export interface LiveComposerTargets {
  presence: HTMLElement | null;
  caption: HTMLElement | null;
  controls: HTMLElement | null;
  details: HTMLElement | null;
}

export interface LiveComposerProps {
  targets: LiveComposerTargets;
  state: LiveState;
  status: string;
  elapsed: string;
  disabled: boolean;
  unavailable: string;
  loading: boolean;
  voice: string;
  settingsOpen: boolean;
  settingsSaving: boolean;
  settingsDialogId?: string;
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
  audio?: AudioSource;
}

/** Keep new speech visible unless the reader has scrolled back within this caption. */
function CurrentCaption({ caption }: { caption: Caption }) {
  const paragraph = useRef<HTMLParagraphElement>(null);
  const following = useRef(true);
  const previousId = useRef(caption.id);
  useLayoutEffect(() => {
    const element = paragraph.current;
    if (!element) return;
    if (previousId.current !== caption.id) {
      previousId.current = caption.id;
      following.current = true;
    }
    if (following.current) element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
  }, [caption.id, caption.text]);
  return <p ref={paragraph} className="voice-current-caption" tabIndex={0} aria-label="Current assistant Live caption"
    onScroll={event => {
      const element = event.currentTarget;
      following.current = element.scrollHeight - element.clientHeight - element.scrollTop <= 4;
    }}>{caption.text.trim()}</p>;
}

/** Stable slots share the actual composer without moving its textarea or media controller. */
export function LiveComposer({ targets, state, status, elapsed, disabled, unavailable, loading, voice, settingsOpen, settingsSaving, settingsDialogId, inspectionOpen, start, end, close, configure, muteInput, muteOutput, play, refresh, viewChat, inspect, inspector, contextOpen = false, showContext, contextDetails, audioNotice, audio }: LiveComposerProps) {
  const connected = state.phase === "connected";
  const active = ["permission", "connecting", "connected", "ending"].includes(state.phase);
  const delegation = currentLiveDelegation(state.delegations);
  const unfinished = delegation?.status === "queued" || delegation?.status === "working";
  const pending = state.delegations.filter(item => item.status === "queued" || item.status === "working").length;
  const delegationLabel = delegation ? !connected && unfinished ? "Assistant still working in chat" : {
    queued: "Request queued", working: "Processing…", completed: "Request complete", failed: "Request failed", cancelled: "Request stopped",
  }[delegation.status] : "";
  const routineEnd = state.phase === "ended" && ["Live session closed. Finalization confirmed.", "Conversation ended. Your microphone is off."].includes(state.notice);
  const feedback = state.error || unavailable || (routineEnd ? "" : state.notice);
  const startDisabled = disabled || settingsSaving || settingsOpen || loading;
  const interactionDisabled = connected ? false : active || startDisabled;
  const caption = state.captions.findLast(item => item.speaker === "assistant" && item.text.trim());
  if (!targets.presence || !targets.caption || !targets.controls || !targets.details) return null;

  return <>
    {createPortal(<div className={`voice-presence phase-${state.phase}`}>
      <LiveSphere phase={state.phase} micMuted={state.micMuted} outputMuted={state.outputMuted}
        busy={!!unfinished && connected} disabled={interactionDisabled}
        inputLevel={state.inputLevel || 0} outputLevel={state.outputLevel || 0}
        audio={audio} sessionId={state.sessionId} delegations={state.delegations}
        onActivate={connected ? muteInput : start} />
    </div>, targets.presence)}
    {createPortal(<div className={`voice-caption phase-${state.phase}`}>
      <span className="byline">Assistant <span className="live-badge"><Icon name="wave" size={11} /> Live</span></span>
      {caption && <CurrentCaption caption={caption} />}
    </div>, targets.caption)}
    {createPortal(<VoiceControls phase={state.phase} status={status} voice={voice} elapsed={elapsed}
      micMuted={state.micMuted} outputMuted={state.outputMuted} startDisabled={startDisabled}
      settingsOpen={settingsOpen} settingsSaving={settingsSaving} settingsDialogId={settingsDialogId} start={start} end={end} close={close}
      configure={configure} muteInput={muteInput} muteOutput={muteOutput} />, targets.controls)}
    {createPortal(<section id="voice-session" className={`voice-composer phase-${state.phase}`} aria-label="Voice in this chat" onKeyDown={event => {
      if (event.key === "Escape" && contextOpen) { event.stopPropagation(); showContext?.(); }
      else if (event.key === "Escape" && inspectionOpen) { event.stopPropagation(); inspect(); }
    }}>
    {state.context && showContext && <button type="button" className="voice-context-trigger" aria-expanded={contextOpen} aria-controls="voice-inline-context" disabled={settingsSaving} onClick={showContext}>{contextLabel(state.context)}</button>}
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
  </section>, targets.details)}
  </>;
}
