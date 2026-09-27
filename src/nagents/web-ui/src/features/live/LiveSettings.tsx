import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Icon } from "../../components/Icon.js";
import { LiveSettingsController, settingsApi } from "./settings.js";
import type { LiveSettingsSnapshot } from "./types.js";

export const providerLabel = (name: string) => ({ openai: "OpenAI", openai_compatible: "OpenAI-compatible", azure_openai_compatible_v1: "Azure OpenAI", foundry: "Azure AI Foundry" })[name] || name;

export function LiveSettings({ token, assistant, blocked, saved, back }: {
  token: string; assistant?: { provider: string; model: string; agent: string };
  blocked: boolean; saved: (snapshot: LiveSettingsSnapshot) => void; back: () => void;
}) {
  const [controller] = useState(() => new LiveSettingsController(settingsApi(token)));
  const heading = useRef<HTMLHeadingElement>(null);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const { values, snapshot, loading, saving, error, needsRefresh } = state;
  const disabled = blocked || loading || saving;
  const configured = snapshot?.source === "providers";
  useEffect(() => {
    void controller.load(); heading.current?.focus();
    return () => controller.dispose();
  }, [controller]);
  useEffect(() => {
    if (!loading && globalThis.matchMedia?.("(max-width: 760px)").matches) heading.current?.scrollIntoView({ block: "start" });
  }, [loading]);
  async function save() { if (blocked) return; const result = await controller.save(); if (result) saved(result); }

  return <section className="live-settings-panel" aria-labelledby="live-settings-title">
    <header><div><Icon name="settings" size={17} /><h3 id="live-settings-title" ref={heading} tabIndex={-1}>Connection settings</h3></div><button type="button" disabled={saving} onClick={back} aria-label="Back to conversation" title="Back to conversation"><Icon name="close" size={16} /></button></header>
    <form className="live-settings-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <p className="live-settings-description">Voice uses your active provider connection. Changes to voice preferences apply to the next conversation.</p>
      {loading && <p role="status">Loading connection settings…</p>}
      {!loading && snapshot && !configured && <p role="status" className="live-settings-feedback">Add a provider in Global or Workspace settings → Provider connections to configure GPT-Live.</p>}
      {values && snapshot && configured && <fieldset disabled={disabled || snapshot.live_supported === false}>
        {snapshot.live_supported === false ? <p>Selected provider {snapshot.profile_name} does not support GPT-Live. Choose OpenAI or Foundry in Provider connections.</p> : <label className="live-enabled"><span><strong>Enable GPT-Live</strong><small>Ready when you choose to connect.</small></span><input type="checkbox" role="switch" checked={values.enabled} onChange={(event) => controller.edit({ enabled: event.target.checked })} /></label>}
        <label>Reasoning backend<select value={values.backend_mode} onChange={(event) => controller.edit({ backend_mode: event.target.value as "assistant" | "hosted" })}><option value="assistant">Main assistant (selected chat provider)</option><option value="hosted">Hosted Responses (separate assistant)</option></select></label>
        {values.backend_mode === "assistant" && <p className="live-backend-note">{assistant ? `${assistant.agent} · ${assistant.provider}: ${assistant.model}.` : "Uses the main assistant selected in workspace settings."} Voice requests join the selected chat's history and use its tools and approval rules. Change its provider and model in normal workspace settings.</p>}
        <p role="status">Using <strong>{snapshot.profile_name}</strong> ({providerLabel(values.provider)}) from {snapshot.connection_scope === "workspace" ? "Workspace" : "Global"} settings. Change its endpoint and authentication in that scope’s Provider connections. {snapshot.auth === "entra" ? "Microsoft Entra ID resolves through DefaultAzureCredential." : `Live uses $${snapshot.api_key_env || "OPENAI_API_KEY"} in the server environment.`} {snapshot.key_configured ? "Credentials are available." : "Set the variable before connecting."}</p>
        <div className="live-settings-models"><label>Voice model<input value={values.model} autoComplete="off" spellCheck={false} maxLength={128} onChange={(event) => controller.edit({ model: event.target.value })} /></label>{values.backend_mode === "hosted" && <label>Backend model<input value={values.backend_model} autoComplete="off" spellCheck={false} maxLength={128} onChange={(event) => controller.edit({ backend_model: event.target.value })} /></label>}</div>
        <label>Default voice<select value={values.voice} onChange={(event) => controller.edit({ voice: event.target.value })}>{snapshot.voices.map((voice) => <option key={voice} value={voice}>{voice.charAt(0).toUpperCase() + voice.slice(1)}</option>)}</select></label>
      </fieldset>}
      {blocked && <p className="live-settings-feedback" role="status">End the active conversation before changing its connection settings.</p>}
      {error && <p className="live-settings-feedback" role="alert">{error}</p>}
      <footer><button type="button" disabled={saving || loading} onClick={() => void controller.load()}>{needsRefresh ? "Reload saved settings" : "Reload"}</button>{configured && <button type="submit" className="live-save" disabled={disabled || !values || needsRefresh || snapshot?.live_supported === false}>{saving ? "Saving…" : "Save voice settings"}</button>}</footer>
    </form>
  </section>;
}
