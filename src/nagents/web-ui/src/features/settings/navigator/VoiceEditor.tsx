import { useEffect } from "react";
import type { ReactNode } from "react";
import { request, RequestError } from "../../../api/client.js";
import { ProviderLogo } from "../../../components/ProviderLogo.js";
import { providerLabel } from "../../live/LiveSettings.js";
import { settingsValidation } from "../../live/settings.js";
import { inheritField, nestedVoice, readJson, sourceLabel, targetQuery, useNavigatorDraft, voiceDraft, voiceWrite } from "./state.js";
import type { NavigatorTarget, NavigatorVoiceReply, VoiceDraft, VoiceProfile } from "./state.js";

export function VoiceEditor({ token, target, report, snapshotChanged, connections }: {
  token: string; target: NavigatorTarget; report: (dirty: boolean) => void;
  snapshotChanged: (reply: NavigatorVoiceReply) => void; connections: () => void;
}) {
  const editor = useNavigatorDraft<NavigatorVoiceReply, VoiceDraft>({
    read: signal => readJson(`live/settings?${targetQuery(target)}`, token, signal), create: voiceDraft,
    write: async (snapshot, draft) => {
      if (target.scope === "agent" && draft.model && !draft.model.provider.trim()) throw new RequestError("Choose a saved connection, or inherit the whole speech model from the workspace.", 422);
      const effective = resolved(snapshot, draft);
      const error = settingsValidation({ ...snapshot.values, ...effective, model: effective.model.id, connection_id: effective.model.provider });
      if (error) throw new RequestError(error, 422);
      return (await request("live/settings", token, voiceWrite(target, snapshot.revision, draft))).json() as Promise<NavigatorVoiceReply>;
    },
  });
  const { snapshot, draft, pending, recovery, error, dirty } = editor;
  useEffect(() => { report(dirty); }, [dirty, report]);
  useEffect(() => { if (snapshot) snapshotChanged(snapshot); }, [snapshot, snapshotChanged]);
  const values = snapshot && draft && resolved(snapshot, draft);
  const disabled = pending || recovery;
  function edit<K extends keyof VoiceProfile>(key: K, value: VoiceProfile[K]) { if (draft) editor.edit({ ...draft, [key]: value }); }
  function field(key: keyof VoiceProfile, title: string, control: ReactNode, help = "") {
    if (!draft || !snapshot) return null;
    const custom = Object.hasOwn(draft, key);
    const origin = dirty && custom ? "Unsaved preference" : custom ? target.scope === "global" ? "Global default" : snapshot.saved_live_present === false && target.scope === "agent" ? "Agent definition" : `${target.scope === "agent" ? "Agent" : "Workspace"} override` : Object.hasOwn(voiceDraft(snapshot), key) ? `Will inherit ${target.scope === "agent" ? "workspace" : "global"}` : sourceLabel(snapshot.origins[key] || (target.scope === "agent" ? "workspace" : "global"));
    return <div className="an-voice-field"><div className="an-field-title"><span>{title}</span><span className="an-field-origin">{origin}{target.scope !== "global" && custom && <button type="button" disabled={disabled} onClick={() => editor.edit(inheritField(draft, key))}>Use {target.scope === "agent" ? "workspace" : "global"}</button>}</span></div>{control}{help && <p className="an-footnote">{help}</p>}</div>;
  }
  return <section className="an-voice-editor">
    <div className="an-section-heading"><div><h3>Voice</h3><p>{target.scope === "agent" ? `How ${target.agent} speaks in live conversations.` : "Speech defaults inherited by agents."}</p></div></div>
    <p className="an-footnote">Saved voice changes apply to the next voice conversation. The current conversation keeps the configuration accepted when it started.</p>
    {pending && !snapshot && <p role="status">Loading voice preferences…</p>}
    {error && <p role="alert" className="error-text">{error}</p>}
    {(recovery || (!snapshot && error)) && <div className="an-recovery"><p>Your draft is kept. Reload saved voice preferences before saving again.</p><button type="button" disabled={pending} onClick={() => void editor.reload()}>{dirty ? "Discard voice draft and reload" : "Reload saved voice settings"}</button></div>}
    {snapshot && draft && values && <>
      <div className="an-ownership"><span>{target.scope === "global" ? "Global voice defaults" : Object.keys(draft).length ? `${Object.keys(draft).length} explicit preferences` : "Voice fully inherited"}</span>{target.scope !== "global" && <button type="button" disabled={disabled || !Object.keys(draft).length} onClick={() => editor.edit({})}>Use {target.scope === "agent" ? "workspace defaults" : "global defaults"}</button>}</div>
      <fieldset className="an-fields" disabled={disabled}>
        <div className="an-voice-pair">
          {field("voice", "Voice", <select aria-label="Voice" value={values.voice} onChange={event => edit("voice", event.target.value)}>{!snapshot.voices.includes(values.voice) && <option value={values.voice}>{values.voice} · unavailable</option>}{snapshot.voices.map(voice => <option key={voice}>{voice}</option>)}</select>)}
          {field("enabled", "Live voice", <label className="an-toggle"><input aria-label="Enable live voice" type="checkbox" checked={values.enabled} onChange={event => edit("enabled", event.target.checked)} />{values.enabled ? "Enabled" : "Disabled"}</label>)}
        </div>
        {field("instructions", "Voice instructions", <textarea aria-label="Voice instructions" rows={4} value={values.instructions} onChange={event => edit("instructions", event.target.value)} placeholder="Keep answers brief and speak at a relaxed pace." />, "Voice instructions guide speech and tone. Agent and workspace instructions continue to apply. Up to 6,000 characters / 12 KB.")}
        <div className="an-form-divider" />
        {field("model", "Speech model", <div className="an-speech-binding">
          <label><span><ProviderLogo provider={snapshot.connections?.find(connection => connection.name === values.model.provider)?.provider || snapshot.values.provider} />Provider connection</span><select aria-label="Voice provider connection" value={values.model.provider} onChange={event => edit("model", { provider: event.target.value, id: "" })}>
            {target.scope === "agent" ? <option value="" disabled>Choose a saved connection</option> : <option value="">Configured default connection</option>}{values.model.provider && !snapshot.connections?.some(connection => connection.name === values.model.provider) && <option value={values.model.provider}>{values.model.provider} · unavailable</option>}
            {snapshot.connections?.map(connection => <option value={connection.name} key={connection.name}>{connection.name} · {providerLabel(connection.provider)} · {connection.scope}</option>)}
          </select></label>
          <label>Speech model ID<input aria-label="Speech model ID" value={values.model.id} autoComplete="off" spellCheck={false} maxLength={128} placeholder="Enter a Live-compatible model" onChange={event => edit("model", { provider: values.model.provider, id: event.target.value })} /></label>
        </div>, "The connection and speech model are saved together. This model handles live audio independently of the conversation model.")}
        {field("context_mode", "Starting context", <select aria-label="Starting context" value={values.context_mode} onChange={event => edit("context_mode", event.target.value as VoiceProfile["context_mode"])}><option value="recent">Recent chat + saved summary</option><option value="summary">Prepare a concise summary</option><option value="none">Start without chat context</option></select>, values.backend_mode === "hosted" ? "Starting context applies to Main assistant. Hosted voice starts without workspace chat history." : "Use the selected chat to prepare the next voice conversation.")}
        <details className="an-source"><summary>Reasoning backend</summary>
          {field("backend_mode", "Reasoning backend", <select aria-label="Reasoning backend" value={values.backend_mode} onChange={event => edit("backend_mode", event.target.value as VoiceProfile["backend_mode"])}><option value="assistant">Main assistant · this chat's agent</option><option value="hosted" disabled={snapshot.voice_auth === "chatgpt"}>Hosted Responses · separate assistant</option></select>, "Main assistant uses this chat's conversation model, tools, and approval rules. Hosted Responses requires an API-key connection.")}
          {values.backend_mode === "hosted" && field("backend_model", "Hosted backend model", <input aria-label="Hosted backend model" value={values.backend_model} onChange={event => edit("backend_model", event.target.value)} />)}
        </details>
      </fieldset>
      <div className="an-connection-status"><ProviderLogo provider={snapshot.values.provider} /><div><strong>Saved voice connection · {snapshot.profile_name || "Default"}</strong><p>{snapshot.live_supported === false ? "This connection does not support Live." : !snapshot.key_configured ? "Sign in or configure credentials before starting voice." : snapshot.voice_auth === "chatgpt" ? "Uses your ChatGPT / Codex login." : snapshot.voice_auth === "entra" ? "Uses Microsoft Entra ID." : "Provider credentials configured."}</p></div><button type="button" onClick={connections}>Manage connections</button></div>
      {target.scope === "agent" && <details className="an-source"><summary>Agent voice definition</summary><pre>{JSON.stringify(snapshot.definition_live || {}, null, 2)}</pre>{snapshot.saved_live_present && <button type="button" disabled={disabled} onClick={() => void editor.save(draft, undefined, async current => (await request("live/settings", token, { scope: "agent", agent: target.agent, revision: current.revision }, undefined, "DELETE")).json() as Promise<NavigatorVoiceReply>)}>Restore voice definition</button>}</details>}
      <footer className="an-save-bar"><span>{dirty ? `Unsaved voice · ${target.scope === "agent" ? target.agent : target.scope}` : editor.notice || "Saved voice preferences"}</span><button type="button" disabled={pending || !dirty} onClick={editor.discard}>Discard voice</button><button type="button" className="primary" disabled={disabled || !dirty || !values.model.id.trim() || (target.scope === "agent" && !!draft.model && !draft.model.provider.trim())} onClick={() => void editor.save()}>{pending ? "Saving…" : "Save voice"}</button></footer>
    </>}
  </section>;
}

export function resolved(snapshot: NavigatorVoiceReply, draft: VoiceDraft): VoiceProfile {
  const inherited = snapshot.scope === "agent" ? snapshot.inherited || nestedVoice(snapshot)
    : snapshot.scope === "workspace" ? nestedVoice({ values: { ...snapshot.values, ...snapshot.global_preferences } }) : nestedVoice(snapshot);
  return { ...inherited, ...draft };
}
