import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { revealAncestors } from "../../components/disclosures.js";
import { scrollWithin } from "../../components/scrollWithin.js";
import { capability, liveApi } from "./api.js";
import { browserMedia, liveSupport } from "./browser.js";
import { currentLiveDelegation, LiveController } from "./controller.js";
import { DelegationInspector } from "./DelegationInspector.js";
import { LiveComposer } from "./LiveComposer.js";
import type { LiveComposerTargets } from "./LiveComposer.js";
import { VoiceContextDetails } from "./VoiceContextDetails.js";
import { OutputDeviceNotice } from "./OutputDeviceNotice.js";
import type { LiveCapability, LivePhase } from "./types.js";
import type { NavigatorAudio } from "../settings/navigator/AgentNavigator.js";
import { settingsChanged } from "../settings/navigator/state.js";

const labels: Record<LivePhase, string> = {
  idle: "Ready for voice", permission: "Allow your microphone", connecting: "Connecting",
  connected: "Live in this chat", ending: "Ending voice", ended: "Voice ended", error: "Disconnected",
};

function duration(seconds: number) {
  return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}

// Voice lives inside the composer. Captions use the same authenticated chat
// event stream as typed messages; opening settings never remounts the call.
export function LiveDialog({ token, sessionId, targets, close, autoStart = true,
  openSettingsInitially = !autoStart, consumeStartIntent, openAgentSettings, audioChanged }: {
  token: string; sessionId: string; close: () => void; autoStart?: boolean;
  targets: LiveComposerTargets;
  openSettingsInitially?: boolean; consumeStartIntent?: () => void;
  openAgentSettings: () => void; audioChanged?: (audio?: NavigatorAudio) => void;
}) {
  const loadingRequest = useRef<AbortController | undefined>(undefined);
  const [controller] = useState(() => new LiveController({ ...liveApi(token), token, media: browserMedia }));
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [config, setConfig] = useState<LiveCapability>();
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [voice, setVoice] = useState("");
  const [now, setNow] = useState(Date.now());
  const [inspectionOpen, setInspectionOpen] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [inspectedId, setInspectedId] = useState("");
  const inspected = state.delegations.find(item => item.id === inspectedId) || currentLiveDelegation(state.delegations);
  const firstLoad = useRef(true);
  const acceptedAudio = useRef({ voice: "", model: "" });
  const startIntent = useRef(autoStart && !openSettingsInitially);
  const active = ["permission", "connecting", "connected", "ending"].includes(state.phase);
  const connected = state.phase === "connected";
  const main = config?.backend_mode === "assistant";
  const occupied = config?.active_session_id && config.active_session_id !== state.sessionId;
  const unavailable = loadError || liveSupport(config?.transport)
    || (config && !config.available ? config.reason : "")
    || (occupied ? "Another voice conversation is active. End it there, then refresh." : "")
    || (main && !sessionId ? "Select a chat before starting voice." : "");
  const elapsed = state.startedAt ? Math.max(0, Math.floor(((state.endedAt || now) - state.startedAt) / 1000)) : 0;
  const status = state.phase === "idle" ? loading ? "Checking voice" : unavailable ? "Setup needed" : labels.idle
    : state.phase === "connecting" && !state.sessionId && config?.context_mode === "summary" ? "Preparing chat brief" : labels[state.phase];

  async function refresh() {
    loadingRequest.current?.abort();
    const abort = new AbortController(); loadingRequest.current = abort;
    setLoading(true); setLoadError("");
    try {
      const next = await capability(token, AbortSignal.any([abort.signal, AbortSignal.timeout(10_000)]));
      if (abort.signal.aborted) return;
      setConfig(next); setVoice(current => active ? current : next.voice);
      if (firstLoad.current) {
        firstLoad.current = false;
        if (openSettingsInitially || !next.enabled || !next.key_configured) openAgentSettings();
      }
      return next;
    } catch (cause) {
      if (!abort.signal.aborted) setLoadError(cause instanceof Error ? cause.message : "Could not load voice settings.");
    } finally { if (!abort.signal.aborted) setLoading(false); }
  }

  function configure() {
    startIntent.current = false;
    setContextOpen(false);
    setInspectionOpen(false);
    openAgentSettings();
  }
  function closeInspection() {
    setInspectionOpen(false);
    requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(".voice-request-trigger")?.focus({ preventScroll: true }));
  }
  function inspect() {
    setContextOpen(false);
    if (inspectionOpen) { closeInspection(); return; }
    setInspectedId(currentLiveDelegation(state.delegations)?.id || ""); setInspectionOpen(true);
  }
  function showContext() {
    if (contextOpen) {
      setContextOpen(false);
      requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(".voice-context-trigger")?.focus({ preventScroll: true }));
      return;
    }
    setInspectionOpen(false); setContextOpen(true);
    requestAnimationFrame(() => document.getElementById("voice-context-title")?.focus({ preventScroll: true }));
  }
  function viewChat(runId: string) {
    const feed = document.querySelector<HTMLElement>(".conversation");
    if (!feed) return;
    const target = [...feed.querySelectorAll<HTMLElement>("[data-run-id]")].filter(element => element.dataset.runId === runId).at(-1);
    const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth";
    if (target) { revealAncestors(target, feed); scrollWithin(feed, target, "nearest", behavior); target.focus({ preventScroll: true }); }
    else { feed.scrollTo({ top: feed.scrollHeight, behavior }); feed.focus({ preventScroll: true }); }
  }

  useEffect(() => {
    // Consume in the parent before any await. A new server token remounts this
    // controller, and that remount must never replay a previous launch click.
    consumeStartIntent?.();
    // Consume the explicit click intent once. Refresh/save/error recovery never
    // reopens the microphone, and an unmounted capability request cannot start it.
    void refresh().then(next => {
      const shouldStart = startIntent.current; startIntent.current = false;
      if (shouldStart && next?.available && !next.active_session_id && !liveSupport(next.transport) && sessionId)
        void controller.start(next.voice, next.revision, sessionId, next.transport);
    });
    const leave = () => { startIntent.current = false; void controller.end(); };
    window.addEventListener("pagehide", leave);
    return () => {
      window.removeEventListener("pagehide", leave);
      startIntent.current = false;
      loadingRequest.current?.abort(); controller.dispose();
    };
  }, [controller]);
  useEffect(() => {
    const updated = () => { void refresh(); };
    window.addEventListener(settingsChanged, updated);
    return () => window.removeEventListener(settingsChanged, updated);
  }, [active]);
  useEffect(() => {
    if (active && !acceptedAudio.current.model) acceptedAudio.current = { voice, model: config?.model || "" };
    if (!active) acceptedAudio.current = { voice: "", model: "" };
    audioChanged?.({ phase: state.phase, active: connected, disabled: ["permission", "connecting", "ending"].includes(state.phase), transport: config?.transport,
      selection: state.devices, voice: active ? acceptedAudio.current.voice : voice, model: active ? acceptedAudio.current.model : config?.model || "",
      applyDevice: connected ? (kind, id) => controller.switchDevice(kind, id) : undefined });
  }, [active, connected, state.phase, state.devices, voice, config?.model, config?.transport, controller, audioChanged]);
  useEffect(() => () => audioChanged?.(undefined), [audioChanged]);
  useEffect(() => {
    if (!connected) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [connected]);
  useEffect(() => { if (state.phase === "ended" || state.phase === "error") void refresh(); }, [state.phase]);
  return <LiveComposer targets={targets} state={state} audio={controller.audio} status={status} elapsed={duration(elapsed)}
    disabled={loading || !config?.available || !!unavailable}
    unavailable={unavailable} loading={loading} voice={voice}
    inspectionOpen={inspectionOpen} inspect={inspect}
    contextOpen={contextOpen} showContext={showContext}
    audioNotice={<OutputDeviceNotice connected={connected} outputId={state.devices.outputId} hidden={false}
      useDefault={() => controller.switchDevice("output", "")} />}
    contextDetails={contextOpen && state.context && <VoiceContextDetails context={state.context} token={token} sessionId={state.sessionId} close={showContext} />}
    inspector={inspectionOpen && inspected && <DelegationInspector token={token} delegation={inspected} delegations={state.delegations} select={setInspectedId} close={closeInspection} viewChat={viewChat} />}
    start={() => { setInspectionOpen(false); setContextOpen(false); if (config) void controller.start(voice, config.revision, sessionId, config.transport); }}
    end={() => { startIntent.current = false; void controller.end(); }} close={() => { startIntent.current = false; close(); }} configure={configure}
    muteInput={() => controller.muteInput()} muteOutput={() => controller.muteOutput()}
    play={() => void controller.play()} refresh={() => { startIntent.current = false; void refresh(); }} viewChat={viewChat} />;
}
