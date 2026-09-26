import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Icon } from "../../components/Icon.js";
import { connectionChanged, LiveSettingsController, settingsApi } from "./settings.js";
import type { LiveSettingsSnapshot } from "./types.js";

export const providerLabel = (name: string) => ({ openai: "OpenAI", openai_compatible: "OpenAI-compatible", azure_openai_compatible_v1: "Azure OpenAI" })[name] || name;

export function LiveSettings({ token, blocked, saved, back }: {
  token: string; blocked: boolean; saved: (snapshot: LiveSettingsSnapshot) => void; back: () => void;
}) {
  const [controller] = useState(() => new LiveSettingsController(settingsApi(token)));
  const heading = useRef<HTMLHeadingElement>(null);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const { values, snapshot, apiKey, clearKey, loading, saving, error, needsRefresh } = state;
  const disabled = blocked || loading || saving;
  const changed = !!(snapshot && values && connectionChanged(snapshot.values, values));
  const hasSavedKey = !!snapshot?.key_configured && !changed && !clearKey;
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
      <p className="live-settings-description">Set up your voice connection here. Changes are saved for this workspace and apply to the next conversation.</p>
      {loading && <p role="status">Loading connection settings…</p>}
      {values && snapshot && <fieldset disabled={disabled}>
        <label className="live-enabled"><span><strong>Enable GPT-Live</strong><small>Ready when you choose to connect.</small></span><input type="checkbox" role="switch" checked={values.enabled} onChange={(event) => controller.edit({ enabled: event.target.checked })} /></label>
        <label>Provider<select value={values.provider} onChange={(event) => controller.edit({ provider: event.target.value })}>{snapshot.providers.map((provider) => <option key={provider} value={provider}>{providerLabel(provider)}</option>)}</select></label>
        <label>API endpoint<input type="url" autoComplete="off" value={values.base_url} placeholder={values.provider === "azure_openai_compatible_v1" ? "https://your-resource.openai.azure.com/openai/v1" : "https://api.openai.com/v1"} maxLength={2048} onChange={(event) => controller.edit({ base_url: event.target.value })} /><small>{values.provider === "azure_openai_compatible_v1" ? "Your Azure v1 API prefix." : "Leave blank for the standard OpenAI endpoint."}</small></label>
        <label>API key<input type="password" autoComplete="new-password" spellCheck={false} value={apiKey} placeholder={hasSavedKey ? "Saved key · leave blank to keep" : "Enter your API key"} maxLength={4096} onChange={(event) => controller.key(event.target.value)} aria-describedby="live-key-help" /><small id="live-key-help">{hasSavedKey ? "A key is saved on the server. Its value is never returned to this form." : "Stored on the server. No environment variables needed."}</small></label>
        {snapshot.key_configured && <label className="live-clear-key"><input type="checkbox" checked={clearKey} onChange={(event) => controller.clearKey(event.target.checked)} />Remove the saved API key</label>}
        {changed && snapshot.key_configured && <p className="live-key-change">This is a different provider or endpoint. Enter its key; the previous connection's key will be removed when you save.</p>}
        <div className="live-settings-models"><label>Voice model<input value={values.model} autoComplete="off" spellCheck={false} maxLength={128} onChange={(event) => controller.edit({ model: event.target.value })} /></label><label>Backend model<input value={values.backend_model} autoComplete="off" spellCheck={false} maxLength={128} onChange={(event) => controller.edit({ backend_model: event.target.value })} /></label></div>
        <label>Default voice<select value={values.voice} onChange={(event) => controller.edit({ voice: event.target.value })}>{snapshot.voices.map((voice) => <option key={voice} value={voice}>{voice.charAt(0).toUpperCase() + voice.slice(1)}</option>)}</select></label>
      </fieldset>}
      {blocked && <p className="live-settings-feedback" role="status">End the active conversation before changing its connection settings.</p>}
      {error && <p className="live-settings-feedback" role="alert">{error}</p>}
      <footer><button type="button" disabled={saving || loading} onClick={() => void controller.load()}>{needsRefresh ? "Reload saved settings" : "Reload"}</button><button type="submit" className="live-save" disabled={disabled || !values || needsRefresh}>{saving ? "Saving…" : "Save connection"}</button></footer>
    </form>
  </section>;
}
