import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import { Icon } from "../../components/Icon.js";
import { AudioDeviceSettings } from "./AudioDeviceSettings.js";
import { LiveSettingsController, settingsApi } from "./settings.js";
import type { AudioDeviceSelection, LiveSettingsSnapshot, LiveTransport, VoicePreferences } from "./types.js";

export const providerLabel = (name: string) => ({ openai: "OpenAI", openai_compatible: "OpenAI-compatible", azure_openai_compatible_v1: "Azure OpenAI", foundry: "Azure AI Foundry" })[name] || name;

export function LiveSettings({ token, assistant, blocked, saved, back, hidden = false, onSavingChange, configureConnection, transport, audioActive = false, audioDisabled = false, audioSelection, applyDevice, showDevices = true }: {
  token: string; assistant?: { provider: string; model: string; agent: string };
  blocked: boolean; saved: (snapshot: LiveSettingsSnapshot) => void; back: () => void;
  hidden?: boolean; onSavingChange?: (saving: boolean) => void; configureConnection?: () => void;
  transport?: LiveTransport;
  audioActive?: boolean; audioDisabled?: boolean;
  audioSelection?: AudioDeviceSelection;
  applyDevice?: (kind: "input" | "output", id: string) => Promise<void>;
  showDevices?: boolean;
}) {
  const [controller] = useState(() => new LiveSettingsController(settingsApi(token)));
  const advanced = useRef<HTMLDetailsElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const { values, snapshot, overrides, scope, loading, saving, dirty, error, needsRefresh } = state;
  const disabled = blocked || loading || saving;
  const chatgptLogin = snapshot?.voice_auth === "chatgpt";
  function preference(key: keyof VoicePreferences, label: string, control: ReactNode) {
    const inherited = scope === "workspace" && !Object.hasOwn(overrides, key);
    return <div className="live-preference" key={key}>
      <label>{label}{control}</label>
      {scope === "workspace" && <div className="live-preference-origin"><label className="live-preference-override"><input type="checkbox" aria-label={`Customize ${label.toLowerCase()} for this workspace`} checked={!inherited} onChange={event => controller.override(key, event.target.checked)} />Customize for this workspace</label>{inherited && <small>Global default</small>}</div>}
    </div>;
  }
  useEffect(() => {
    void controller.load();
    return () => controller.dispose();
  }, [controller]);
  useEffect(() => {
    if (hidden) return;
    heading.current?.focus({ preventScroll: true });
  }, [hidden]);
  useEffect(() => { onSavingChange?.(saving); }, [saving, onSavingChange]);
  useEffect(() => {
    if (advanced.current && (error || (snapshot && (!snapshot.values.enabled || !snapshot.key_configured || snapshot.live_supported === false || (snapshot.voice_auth === "chatgpt" && snapshot.values.backend_mode === "hosted"))))) advanced.current.open = true;
  }, [snapshot, error]);
  async function save() { if (blocked) return; const result = await controller.save(); if (result) saved(result); }

  return <section className="live-settings-panel" hidden={hidden} aria-labelledby="live-settings-title">
    <header><div><Icon name="settings" size={17} /><h3 id="live-settings-title" ref={heading} tabIndex={-1}>Voice settings</h3></div><button type="button" disabled={saving} onClick={back} aria-label="Back to conversation" title="Back to conversation"><Icon name="close" size={16} /></button></header>
    <form className="live-settings-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <p className="live-settings-description">Choose your default voice, behavior, and starting context.</p>
      {showDevices && <AudioDeviceSettings transport={transport || "websocket"} disabled={audioDisabled} active={audioActive} activeSelection={audioSelection} applyDevice={applyDevice} />}
      {loading && <p role="status">Loading Voice settings…</p>}
      {values && snapshot && <fieldset className="live-voice-defaults" disabled={disabled}>
          <legend className="sr-only">Voice defaults</legend>
          <div className="live-settings-group-heading"><h4>Your voice</h4><span>Next conversation</span></div>
          {preference("voice", "Default voice", <select value={values.voice} disabled={scope === "workspace" && !Object.hasOwn(overrides, "voice")} onChange={event => controller.edit({ voice: event.target.value as VoicePreferences["voice"] })}>{!snapshot.voices.includes(values.voice) && <option value={values.voice}>{values.voice} · unavailable for this connection</option>}{snapshot.voices.map(voice => <option key={voice} value={voice}>{voice.charAt(0).toUpperCase() + voice.slice(1)}</option>)}</select>)}
          {preference("instructions", "Voice instructions", <textarea className="live-voice-instructions" value={values.instructions ?? ""} rows={3}
            placeholder="For example: Keep answers brief and speak at a relaxed pace."
            aria-describedby="live-instructions-help" disabled={scope === "workspace" && !Object.hasOwn(overrides, "instructions")}
            onChange={event => controller.edit({ instructions: event.target.value })} />)}
          <small id="live-instructions-help">Guide tone, pace, and response style for the next conversation. {Array.from(values.instructions ?? "").length.toLocaleString()} / 6,000 characters; 12 KB text limit.</small>
          {preference("context_mode", "Starting context", <select value={values.context_mode ?? "recent"} disabled={scope === "workspace" && !Object.hasOwn(overrides, "context_mode")}
            onChange={event => controller.edit({ context_mode: event.target.value as VoicePreferences["context_mode"] })}>
            <option value="recent">Recent chat + saved summary</option><option value="summary">Prepare a concise summary</option><option value="none">Start without chat context</option>
          </select>)}
          <small>{values.backend_mode === "hosted" ? "Starting context applies to Main assistant. Hosted voice starts without the workspace chat's history." : values.context_mode === "none" ? "Voice starts fresh. Delegated requests still use your saved chat." : values.context_mode === "summary" ? "Prepare a short briefing of this chat before voice starts." : "Use recent messages and any saved summary from this chat."}</small>
      </fieldset>}
      <label className="live-settings-scope">Apply settings to<select value={scope} disabled={disabled || dirty} onChange={event => controller.setScope(event.target.value as "global" | "workspace")}><option value="global">Global · all workspaces</option><option value="workspace">This workspace · customize defaults</option></select></label>
      {dirty && <small className="live-settings-draft-note" role="status">Save or reload these changes before switching scope.</small>}
      {values && snapshot && <details className="live-settings-advanced" ref={advanced}>
          <summary><span>Connection &amp; model</span><Icon name="chevron" size={15} /></summary>
          <fieldset disabled={disabled}>
            <legend className="sr-only">Connection and model preferences</legend>
        {preference("enabled", "Enable GPT-Live", <input type="checkbox" role="switch" checked={values.enabled} disabled={scope === "workspace" && !Object.hasOwn(overrides, "enabled")} onChange={event => controller.edit({ enabled: event.target.checked })} />)}
        {preference("connection_id", "Voice connection", <select value={values.connection_id || ""} disabled={scope === "workspace" && !Object.hasOwn(overrides, "connection_id")} onChange={event => controller.edit({ connection_id: event.target.value })}>
          <option value="">Use configured default provider</option>
          {values.connection_id && !snapshot.connections?.some(connection => connection.name === values.connection_id) && <option value={values.connection_id}>{values.connection_id} · unavailable</option>}
          {snapshot.connections?.map(connection => <option key={connection.name} value={connection.name}>{connection.name} · {providerLabel(connection.provider)} ({connection.scope})</option>)}
        </select>)}
        <small>Uses the normal provider configuration and its saved authentication. Leave the default selected or choose a Live-compatible connection. Microphone audio and playback connect only to ngn serve.</small>
        {preference("backend_mode", "Reasoning backend", <select value={values.backend_mode} disabled={scope === "workspace" && !Object.hasOwn(overrides, "backend_mode")} onChange={event => controller.edit({ backend_mode: event.target.value as "assistant" | "hosted" })}><option value="assistant">Main assistant (selected chat provider)</option><option value="hosted" disabled={chatgptLogin}>Hosted Responses (separate assistant)</option></select>)}
        {chatgptLogin && <small>ChatGPT/Codex login connects GPT-Live to your main assistant. Hosted Responses requires an API-key connection.</small>}
        {values.backend_mode === "assistant" && <p className="live-backend-note">{assistant ? `${assistant.agent} · ${assistant.provider}: ${assistant.model}.` : "Uses the main assistant selected in workspace settings."} Voice requests join the selected chat's history and use its tools and approval rules. Change its provider and model in normal workspace settings.</p>}
        <div className={`live-settings-models ${values.backend_mode === "assistant" ? "single" : ""}`}>
          {preference("model", "GPT-Live model", <input value={values.model} disabled={scope === "workspace" && !Object.hasOwn(overrides, "model")} autoComplete="off" spellCheck={false} maxLength={128} onChange={event => controller.edit({ model: event.target.value })} />)}
          {values.backend_mode === "hosted" && preference("backend_model", "Hosted backend model", <input value={values.backend_model} disabled={scope === "workspace" && !Object.hasOwn(overrides, "backend_model")} autoComplete="off" spellCheck={false} maxLength={128} onChange={event => controller.edit({ backend_model: event.target.value })} />)}
        </div>
        <div className="live-connection-summary" role="status"><strong>Saved voice connection</strong>
          {snapshot.profile_name ? <p><span>{snapshot.profile_name} · {providerLabel(snapshot.values.provider)}</span><br />{snapshot.live_supported === false ? "Choose a connection that supports GPT-Live." : chatgptLogin ? snapshot.key_configured ? "Uses your existing ChatGPT/Codex login on the server." : "Sign in to ChatGPT/Codex for this provider connection before connecting." : snapshot.voice_auth === "entra" ? snapshot.key_configured ? "Uses Microsoft Entra ID on the server." : "Configure Microsoft Entra ID for this provider before connecting." : snapshot.key_configured ? "Uses the selected provider’s API credentials on the server." : "Configure credentials for the selected provider before connecting."}</p> : <p>Configure a provider connection that supports Live to use voice.</p>}
          {dirty && <small>Your connection selection applies after saving.</small>}
          {configureConnection && <button type="button" disabled={dirty} onClick={configureConnection}>Manage provider connections</button>}
        </div>
          </fieldset>
      </details>}
      {blocked && <p className="live-settings-feedback" role="status">End the active conversation before changing voice behavior, starting context, connection, or model.</p>}
      {error && <p className="live-settings-feedback" role="alert">{error}</p>}
      <footer><button type="button" disabled={saving || loading} onClick={() => void controller.load()}>{needsRefresh ? "Reload saved settings" : "Reload"}</button><button type="submit" className="live-save" disabled={disabled || !values || needsRefresh}>{saving ? "Saving…" : "Save settings"}</button></footer>
    </form>
  </section>;
}
