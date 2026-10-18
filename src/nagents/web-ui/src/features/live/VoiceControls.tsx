import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import type { LivePhase } from "./types.js";

export interface VoiceControlsProps {
  phase: LivePhase;
  status: string;
  voice: string;
  elapsed: string;
  micMuted: boolean;
  outputMuted: boolean;
  startDisabled: boolean;
  start(): void;
  end(): void;
  close(): void;
  configure(): void;
  muteInput(): void;
  muteOutput(): void;
}

/** Expanding the capsule changes the controls, never the connection or audio. */
export function VoiceControls({ phase, status, voice, elapsed, micMuted, outputMuted, startDisabled, start, end, close, configure, muteInput, muteOutput }: VoiceControlsProps) {
  const [expanded, setExpanded] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const connected = phase === "connected";
  const active = connected || phase === "permission" || phase === "connecting" || phase === "ending";
  const label = connected ? "Live" : status || (phase === "ended" ? "Stopped" : "Voice");
  const startLabel = phase === "error" ? "Retry" : phase === "ended" ? "Start again" : "Start voice";
  const endLabel = connected ? "End voice" : phase === "ending" ? "Ending voice" : "Cancel connection";
  const microphoneLabel = micMuted ? "Unmute microphone" : "Mute microphone";
  const speakerLabel = outputMuted ? "Unmute speaker" : "Mute speaker";
  const collapse = () => {
    setExpanded(false);
    trigger.current?.focus({ preventScroll: true });
  };

  useEffect(() => {
    if (!expanded) return;
    const owner = root.current?.ownerDocument;
    if (!owner) return;
    const dismiss = (event: Event) => {
      const target = event.target;
      const element = owner.defaultView?.Element;
      // Dialog focus belongs to the modal, including its return to Settings.
      if (element && target instanceof element && !root.current?.contains(target) && !target.closest("dialog")) setExpanded(false);
    };
    owner.addEventListener("pointerdown", dismiss);
    owner.addEventListener("focusin", dismiss);
    return () => {
      owner.removeEventListener("pointerdown", dismiss);
      owner.removeEventListener("focusin", dismiss);
    };
  }, [expanded]);

  return <div ref={root} className="voice-controls" data-expanded={expanded} data-active={active} data-muted={micMuted} data-phase={phase}
    onKeyDown={event => {
      if (event.key === "Escape" && expanded) {
        event.preventDefault();
        event.stopPropagation();
        collapse();
      }
    }}>
    <button ref={trigger} type="button" className="voice-controls-trigger" aria-expanded={expanded} aria-controls={panelId}
      aria-label={`${expanded ? "Hide" : "Show"} voice controls · ${label}`}
      title={[label, voice, elapsed].filter(Boolean).join(" · ")}
      onClick={() => setExpanded(value => !value)}>
      <Icon name="wave" size={16} /><span className="voice-controls-label" aria-hidden="true">{label}</span>
    </button>
    <span className="sr-only" role="status">{label}</span>
    <div className="voice-controls-panel" id={panelId} aria-hidden={!expanded} inert={!expanded}>
      <div className="voice-controls-panel-inner"><div className="voice-controls-actions" role="group" aria-label="Voice controls">
        <button type="button" className="voice-mic" aria-label={microphoneLabel} title={microphoneLabel} aria-pressed={micMuted} disabled={!connected} onClick={muteInput}><Icon name={micMuted ? "mic-off" : "mic"} size={16} /></button>
        <button type="button" className="voice-speaker" aria-label={speakerLabel} title={speakerLabel} aria-pressed={outputMuted} disabled={!connected} onClick={muteOutput}><Icon name={outputMuted ? "volume-off" : "volume"} size={16} /></button>
        <button type="button" className="voice-settings-trigger" aria-label="Audio devices and voice settings" title="Audio devices and voice settings" aria-haspopup="dialog" aria-controls="agent-navigator-dialog" onClick={configure}><Icon name="settings" size={16} /></button>
        <span className="voice-controls-divider" aria-hidden="true" />
        {active ? <button type="button" className="voice-stop" aria-label={endLabel} title={endLabel} disabled={phase === "ending"} onClick={end}><Icon name="stop" size={14} /><span>{connected ? "Stop" : phase === "ending" ? "Ending" : "Cancel"}</span></button>
          : <button type="button" className="voice-start" aria-label={startLabel} title={startLabel} disabled={startDisabled} onClick={start}><Icon name="wave" size={14} /><span>{startLabel}</span></button>}
        <button type="button" className="voice-dismiss" aria-label="Close voice controls" title="Close voice and dismiss the sphere" onClick={close}><Icon name="close" size={15} /></button>
      </div></div>
    </div>
  </div>;
}
