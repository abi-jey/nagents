import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { revealAncestors } from "../../components/disclosures.js";
import { scrollWithin } from "../../components/scrollWithin.js";
import { capability, liveApi } from "./api.js";
import { browserMedia, liveSupport } from "./browser.js";
import { currentLiveDelegation, LiveController } from "./controller.js";
import { DelegationInspector } from "./DelegationInspector.js";
import { LiveComposer } from "./LiveComposer.js";
import { VoiceSettingsDialog } from "./VoiceSettingsDialog.js";
import { VoiceContextDetails } from "./VoiceContextDetails.js";
import { OutputDeviceNotice } from "./OutputDeviceNotice.js";
import { LiveSettings } from "./LiveSettings.js";
import type { LiveCapability, LivePhase, LiveSettingsSnapshot } from "./types.js";

const labels: Record<LivePhase, string> = {
  idle: "Ready for voice", permission: "Allow your microphone", connecting: "Connecting",
  connected: "Live in this chat", ending: "Ending voice", ended: "Voice ended", error: "Disconnected",
};

function duration(seconds: number) {
  return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}

// Voice lives inside the composer. Captions use the same authenticated chat
// event stream as typed messages; opening settings never remounts the call.
export function LiveDialog({ token, sessionId, close, configureConnection, autoStart = true,
  openSettingsInitially = !autoStart, consumeStartIntent }: {
  token: string; sessionId: string; close: () => void; configureConnection?: () => void; autoStart?: boolean;
  openSettingsInitially?: boolean; consumeStartIntent?: () => void;
}) {
  const loadingRequest = useRef<AbortController | undefined>(undefined);
  const [controller] = useState(() => new LiveController({ ...liveApi(token), token, media: browserMedia }));
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [config, setConfig] = useState<LiveCapability>();
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [voice, setVoice] = useState("");
  const [now, setNow] = useState(Date.now());
  const [settingsOpen, setSettingsOpen] = useState(openSettingsInitially);
  const [settingsVisited, setSettingsVisited] = useState(openSettingsInitially);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [inspectionOpen, setInspectionOpen] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [inspectedId, setInspectedId] = useState("");
  const inspected = state.delegations.find(item => item.id === inspectedId) || currentLiveDelegation(state.delegations);
  const firstLoad = useRef(true);
  const startIntent = useRef(autoStart);
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
      setConfig(next); setVoice(current => next.voices.includes(current) ? current : next.voice);
      if (firstLoad.current) {
        firstLoad.current = false;
        if (!next.enabled || !next.key_configured) { setSettingsVisited(true); setSettingsOpen(true); }
      }
      return next;
    } catch (cause) {
      if (!abort.signal.aborted) setLoadError(cause instanceof Error ? cause.message : "Could not load voice settings.");
    } finally { if (!abort.signal.aborted) setLoading(false); }
  }

  async function saved(snapshot: LiveSettingsSnapshot) {
    setVoice(snapshot.values.voice);
    const next = await refresh();
    if (next?.available) {
      setSettingsOpen(false);
      requestAnimationFrame(() => document.querySelector<HTMLButtonElement>("#voice-session .voice-start")?.focus());
    }
  }
  function configure() {
    startIntent.current = false;
    setContextOpen(false);
    if (settingsOpen) { closeSettings(); return; }
    setInspectionOpen(false); setSettingsVisited(true); setSettingsOpen(true);
  }
  function closeInspection() {
    setInspectionOpen(false);
    requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(".voice-request-trigger")?.focus({ preventScroll: true }));
  }
  function inspect() {
    setContextOpen(false);
    if (inspectionOpen) { closeInspection(); return; }
    setSettingsOpen(false); setInspectedId(currentLiveDelegation(state.delegations)?.id || ""); setInspectionOpen(true);
  }
  function closeSettings() {
    setSettingsOpen(false);
  }
  function showContext() {
    if (contextOpen) {
      setContextOpen(false);
      requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(".voice-context-trigger")?.focus({ preventScroll: true }));
      return;
    }
    setSettingsOpen(false); setInspectionOpen(false); setContextOpen(true);
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
    if (!connected) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [connected]);
  useEffect(() => { if (state.phase === "ended" || state.phase === "error") void refresh(); }, [state.phase]);
  return <><LiveComposer state={state} audio={controller.audio} status={status} elapsed={duration(elapsed)}
    disabled={settingsOpen || settingsSaving || loading || !config?.available || !!unavailable}
    unavailable={unavailable} loading={loading} voice={voice}
    settingsOpen={settingsOpen} settingsSaving={settingsSaving} inspectionOpen={inspectionOpen} inspect={inspect}
    contextOpen={contextOpen} showContext={showContext}
    audioNotice={<OutputDeviceNotice connected={connected} outputId={state.devices.outputId} hidden={settingsOpen}
      useDefault={() => controller.switchDevice("output", "")} />}
    contextDetails={contextOpen && state.context && <VoiceContextDetails context={state.context} token={token} sessionId={state.sessionId} close={showContext} />}
    inspector={inspectionOpen && inspected && <DelegationInspector token={token} delegation={inspected} delegations={state.delegations} select={setInspectedId} close={closeInspection} viewChat={viewChat} />}
    start={() => { setInspectionOpen(false); setContextOpen(false); if (config) void controller.start(voice, config.revision, sessionId, config.transport); }}
    end={() => { startIntent.current = false; void controller.end(); }} close={() => { if (!settingsSaving) { startIntent.current = false; close(); } }} configure={configure}
    muteInput={() => controller.muteInput()} muteOutput={() => controller.muteOutput()}
    play={() => void controller.play()} refresh={() => { startIntent.current = false; void refresh(); }} viewChat={viewChat} />
    {settingsVisited && <VoiceSettingsDialog open={settingsOpen} saving={settingsSaving} close={closeSettings}>
      <LiveSettings token={token} assistant={config?.assistant} transport={config?.transport}
        audioDisabled={["permission", "connecting", "ending"].includes(state.phase)} audioActive={connected} audioSelection={state.devices}
        applyDevice={connected ? (kind, id) => controller.switchDevice(kind, id) : undefined}
        hidden={!settingsOpen} onSavingChange={setSettingsSaving}
        configureConnection={configureConnection && (() => { closeSettings(); configureConnection(); })}
        blocked={active || !!config?.active_session_id} saved={snapshot => void saved(snapshot)} back={closeSettings} />
    </VoiceSettingsDialog>}
  </>;
}
