import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { capability, liveApi } from "./api.js";
import { browserMedia, liveSupport } from "./browser.js";
import { LiveController } from "./controller.js";
import { LiveDock } from "./LiveDock.js";
import { LiveSettings, providerLabel } from "./LiveSettings.js";
import type { LiveCapability, LivePhase, LiveSettingsSnapshot } from "./types.js";
import "./live.css";
import "./dock.css";

const labels: Record<LivePhase, string> = {
  idle: "Ready for voice", permission: "Allow your microphone", connecting: "Connecting",
  connected: "Live in this chat", ending: "Ending voice", ended: "Voice ended", error: "Disconnected",
};

function duration(seconds: number) {
  return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}

// Voice is a nonmodal layer over the current chat. Captions arrive through the
// same authenticated chat event stream as normal messages and saved history.
export function LiveDialog({ token, sessionId, close, configureConnection }: {
  token: string; sessionId: string; close: () => void; configureConnection?: () => void;
}) {
  const loadingRequest = useRef<AbortController | undefined>(undefined);
  const [controller] = useState(() => new LiveController({ ...liveApi(token), token, media: browserMedia }));
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [config, setConfig] = useState<LiveCapability>();
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [voice, setVoice] = useState("");
  const [now, setNow] = useState(Date.now());
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsVisited, setSettingsVisited] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const firstLoad = useRef(true);
  const active = ["permission", "connecting", "connected", "ending"].includes(state.phase);
  const connected = state.phase === "connected";
  const main = config?.backend_mode === "assistant";
  const occupied = config?.active_session_id && config.active_session_id !== state.sessionId;
  const unavailable = loadError || liveSupport(config?.transport)
    || (config && !config.available ? config.reason : "")
    || (occupied ? "Another voice conversation is active. End it there, then refresh." : "")
    || (main && !sessionId ? "Select a chat before starting voice." : "");
  const elapsed = state.startedAt ? Math.max(0, Math.floor(((state.endedAt || now) - state.startedAt) / 1000)) : 0;
  const status = state.phase === "idle" ? loading ? "Checking voice" : unavailable ? "Setup needed" : labels.idle : labels[state.phase];
  const backend = main ? `${config.assistant.agent} · ${config.assistant.model}` : config?.backend_model || "Hosted Responses";
  const provider = config?.voice_auth === "chatgpt" ? "ChatGPT login" : providerLabel(config?.provider || "OpenAI");

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
      requestAnimationFrame(() => document.querySelector<HTMLButtonElement>("#live-voice-dock .live-dock-start")?.focus());
    }
  }
  function configure() {
    setSettingsVisited(true); setSettingsOpen(!settingsOpen);
    if (!settingsOpen) requestAnimationFrame(() => document.getElementById("live-settings-title")?.focus({ preventScroll: true }));
  }
  function closeSettings() {
    setSettingsOpen(false);
    requestAnimationFrame(() => document.querySelector<HTMLButtonElement>("#live-voice-dock [aria-label='Voice settings']")?.focus());
  }

  useEffect(() => {
    document.getElementById("live-dock-title")?.focus({ preventScroll: true });
    void refresh();
    const leave = () => { void controller.end(); };
    window.addEventListener("pagehide", leave);
    return () => {
      window.removeEventListener("pagehide", leave);
      loadingRequest.current?.abort(); controller.dispose();
    };
  }, [controller]);
  useEffect(() => {
    const dock = document.getElementById("live-voice-dock");
    const surface = dock?.querySelector<HTMLElement>(".live-dock-surface");
    const shell = dock?.closest<HTMLElement>(".app-shell");
    if (!dock || !surface || !shell) return;
    const composer = shell.querySelector<HTMLElement>(".composer-area");
    const measure = () => {
      const bottom = composer?.getBoundingClientRect().top ?? window.innerHeight;
      shell.style.setProperty("--live-dock-max-height", `${Math.max(96, Math.floor(bottom - shell.getBoundingClientRect().top - dock.offsetTop - 12))}px`);
      shell.style.setProperty("--live-dock-clearance", `${Math.ceil(surface.getBoundingClientRect().height) + 28}px`);
    };
    measure();
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : undefined;
    observer?.observe(surface);
    if (composer) observer?.observe(composer);
    window.addEventListener("resize", measure);
    return () => { observer?.disconnect(); window.removeEventListener("resize", measure); shell.style.removeProperty("--live-dock-clearance"); shell.style.removeProperty("--live-dock-max-height"); };
  }, []);
  useEffect(() => {
    if (!connected) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [connected]);
  useEffect(() => { if (state.phase === "ended" || state.phase === "error") void refresh(); }, [state.phase]);

  const subtitle = state.phase === "permission" ? "Allow microphone access to speak in this chat."
    : state.phase === "connecting" ? "Bringing your voice into the conversation."
      : connected ? state.micMuted ? "Microphone muted. You can still listen." : "Speak freely. You can keep typing, too."
        : state.phase === "ending" ? "Microphone off. Finishing the connection."
          : state.phase === "ended" ? "Your conversation stays right here."
            : state.phase === "error" ? "Your microphone is off. Reconnect when ready."
              : "Same conversation. Now with your voice.";

  return <LiveDock state={state} status={status} elapsed={duration(elapsed)} subtitle={subtitle}
    disabled={settingsOpen || settingsSaving || loading || !config?.available || !!unavailable}
    unavailable={unavailable} loading={loading} voice={voice} provider={provider} backend={backend}
    settingsOpen={settingsOpen} settingsSaving={settingsSaving}
    start={() => { if (config) void controller.start(voice, config.revision, sessionId, config.transport); }}
    end={() => void controller.end()} close={() => { if (!settingsSaving) close(); }} configure={configure}
    muteInput={() => controller.muteInput()} muteOutput={() => controller.muteOutput()}
    play={() => void controller.play()} refresh={() => void refresh()}>
    {settingsVisited && <LiveSettings token={token} assistant={config?.assistant} transport={config?.transport}
      hidden={!settingsOpen} onSavingChange={setSettingsSaving} configureConnection={configureConnection}
      blocked={active || !!config?.active_session_id} saved={snapshot => void saved(snapshot)} back={closeSettings} />}
  </LiveDock>;
}
