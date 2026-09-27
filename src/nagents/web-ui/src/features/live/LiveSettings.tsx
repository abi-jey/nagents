import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import { Icon } from "../../components/Icon.js";
import { LiveSettingsController, settingsApi } from "./settings.js";
import type { LiveSettingsSnapshot, VoicePreferences } from "./types.js";

export const providerLabel = (name: string) => ({ openai: "OpenAI", openai_compatible: "OpenAI-compatible", azure_openai_compatible_v1: "Azure OpenAI", foundry: "Azure AI Foundry" })[name] || name;

export function LiveSettings({ token, assistant, blocked, saved, back }: {
  token: string; assistant?: { provider: string; model: string; agent: string };
  blocked: boolean; saved: (snapshot: LiveSettingsSnapshot) => void; back: () => void;
}) {
  const [controller] = useState(() => new LiveSettingsController(settingsApi(token)));
  const heading = useRef<HTMLHeadingElement>(null);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const { values, snapshot, overrides, scope, loading, saving, dirty, error, needsRefresh } = state;
  const disabled = blocked || loading || saving;
  function preference(key: keyof VoicePreferences, label: string, control: ReactNode) {
    const inherited = scope === "workspace" && !Object.hasOwn(overrides, key);
    return <div className="live-preference" key={key}>
      {scope === "workspace" && <label className="live-preference-override"><input type="checkbox" checked={!inherited} onChange={event => controller.override(key, event.target.checked)} />Override {label.toLowerCase()} in this workspace</label>}
      <label>{label}{control}</label>
      {inherited && <small>Inherited from Global Voice defaults. Changes there will apply here.</small>}
    </div>;
  }
  useEffect(() => {
    void controller.load(); heading.current?.focus();
    return () => controller.dispose();
  }, [controller]);
  useEffect(() => {
    if (!loading && globalThis.matchMedia?.("(max-width: 760px)").matches) heading.current?.scrollIntoView({ block: "start" });
  }, [loading]);
  async function save() { if (blocked) return; const result = await controller.save(); if (result) saved(result); }

  return <section className="live-settings-panel" aria-labelledby="live-settings-title">
    <header><div><Icon name="settings" size={17} /><h3 id="live-settings-title" ref={heading} tabIndex={-1}>Voice settings</h3></div><button type="button" disabled={saving} onClick={back} aria-label="Back to conversation" title="Back to conversation"><Icon name="close" size={16} /></button></header>
    <form className="live-settings-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <p className="live-settings-description">Voice preferences are independent of provider connections and chat models. Changes apply to the next conversation.</p>
      <label>Editing Voice defaults for<select value={scope} disabled={saving || dirty} onChange={event => controller.setScope(event.target.value as "global" | "workspace")}><option value="global">Global · all workspaces</option><option value="workspace">This workspace · overrides Global</option></select></label>
      {dirty && <small role="status">Save or reload these changes before switching scope.</small>}
      {loading && <p role="status">Loading Voice settings…</p>}
      {values && snapshot && <fieldset disabled={disabled}>
        {preference("enabled", "Enable voice duplex", <input type="checkbox" role="switch" checked={values.enabled} disabled={scope === "workspace" && !Object.hasOwn(overrides, "enabled")} onChange={event => controller.edit({ enabled: event.target.checked })} />)}
        {preference("backend_mode", "Reasoning backend", <select value={values.backend_mode} disabled={scope === "workspace" && !Object.hasOwn(overrides, "backend_mode")} onChange={event => controller.edit({ backend_mode: event.target.value as "assistant" | "hosted" })}><option value="assistant">Main assistant (selected chat provider)</option><option value="hosted">Hosted Responses (separate assistant)</option></select>)}
        {values.backend_mode === "assistant" && <p className="live-backend-note">{assistant ? `${assistant.agent} · ${assistant.provider}: ${assistant.model}.` : "Uses the main assistant selected in workspace settings."} Voice requests join the selected chat's history and use its tools and approval rules. Change its provider and model in normal workspace settings.</p>}
        <div className="live-settings-models">
          {preference("model", "Voice duplex model", <input value={values.model} disabled={scope === "workspace" && !Object.hasOwn(overrides, "model")} autoComplete="off" spellCheck={false} maxLength={128} onChange={event => controller.edit({ model: event.target.value })} />)}
          {preference("backend_model", "Hosted backend model", <input value={values.backend_model} disabled={scope === "workspace" && !Object.hasOwn(overrides, "backend_model")} autoComplete="off" spellCheck={false} maxLength={128} onChange={event => controller.edit({ backend_model: event.target.value })} />)}
        </div>
        {preference("voice", "Default voice", <select value={values.voice} disabled={scope === "workspace" && !Object.hasOwn(overrides, "voice")} onChange={event => controller.edit({ voice: event.target.value as VoicePreferences["voice"] })}>{snapshot.voices.map(voice => <option key={voice} value={voice}>{voice.charAt(0).toUpperCase() + voice.slice(1)}</option>)}</select>)}
        {snapshot.profile_name ? <p role="status">Calls use <strong>{snapshot.profile_name}</strong> ({providerLabel(values.provider)}) from {snapshot.connection_scope === "workspace" ? "Workspace" : "Global"} Provider connections for endpoint and authentication. {snapshot.live_supported === false ? "Choose a Live-capable connection before calling." : snapshot.auth === "entra" ? "Microsoft Entra ID resolves through DefaultAzureCredential." : `Live uses $${snapshot.api_key_env || "OPENAI_API_KEY"} in the server environment.`} {snapshot.key_configured ? "Credentials are available." : "Credentials are not yet available."}</p> : <p role="status">Add an OpenAI or Foundry provider in Provider connections before calling.</p>}
      </fieldset>}
      {blocked && <p className="live-settings-feedback" role="status">End the active conversation before changing Voice settings.</p>}
      {error && <p className="live-settings-feedback" role="alert">{error}</p>}
      <footer><button type="button" disabled={saving || loading} onClick={() => void controller.load()}>{needsRefresh ? "Reload saved settings" : "Reload"}</button><button type="submit" className="live-save" disabled={disabled || !values || needsRefresh}>{saving ? "Saving…" : `Save ${scope === "global" ? "global" : "workspace"} Voice settings`}</button></footer>
    </form>
  </section>;
}
