import { useEffect, useState } from "react";
import { request } from "../../api/client.js";
import type { SettingsScope } from "./transport.js";

type Live = { enabled: boolean; model: string; backend_model: string; voice: string; backend_mode: string };
type Profile = {
  kind: string; auth: string; base_url: string; api: string;
  api_key_env: string; api_version: string; scope: string; live: Live;
  key_configured?: boolean; credential_source?: string; effective_endpoint?: string;
};
type Kind = {
  label: string; auth: string[]; apis: string[]; env: string;
  endpoint_required: boolean; version_required: boolean; live: boolean;
};
type Registry = {
  revision: string; active: string; providers: Record<string, Profile>; kinds: Record<string, Kind>;
  scope: SettingsScope; path: string; origins?: Record<string, SettingsScope>;
  inherited_active?: boolean; global_active?: string;
};

function defaultProfile(kind: string, specs: Record<string, Kind>): Profile {
  return {
    kind, auth: specs[kind].auth[0], base_url: "", api: specs[kind].apis[0],
    api_key_env: "", api_version: "", scope: "https://ai.azure.com/.default",
    live: { enabled: false, model: "gpt-live-1", backend_model: "gpt-5.6-luna", voice: "marin", backend_mode: specs[kind].live ? "assistant" : "hosted" },
  };
}

function editable(profile: Profile): Profile {
  return { kind: profile.kind, auth: profile.auth, base_url: profile.base_url,
    api: profile.api, api_key_env: profile.api_key_env, api_version: profile.api_version,
    scope: profile.scope, live: { ...profile.live } };
}

export function ProvidersPanel({ token, blocked, applied, scope }: {
  token: string; blocked: boolean; applied: () => void; scope: SettingsScope;
}) {
  const [registry, setRegistry] = useState<Registry>();
  const [name, setName] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Profile>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const endpoint = `provider-scopes/${scope}/providers`;
  async function refresh() {
    const value = (await (await request(endpoint, token)).json()) as Registry;
    setRegistry(value);
    setError("");
  }
  useEffect(() => {
    setRegistry(undefined); setDraft(undefined); setName("");
    void refresh().catch(() => setError("Could not load provider connections."));
  }, [token, scope]);

  async function action(run: () => Promise<Registry>, message: string) {
    if (busy || blocked) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const next = await run();
      setRegistry(next);
      setNotice(message);
      if (next.active !== registry?.active || (editing && next.active === name)) applied();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Provider operation failed; reload connections.");
    } finally { setBusy(false); }
  }
  if (!registry) return <section className="settings-group" aria-label="Provider connections"><h3>Provider connections</h3><p role="status">{error || "Loading connections…"}</p></section>;
  const spec = draft ? registry.kinds[draft.kind] : undefined;
  const edit = (change: Partial<Profile>) => setDraft(current => current && { ...current, ...change });
  const editLive = (change: Partial<Live>) => setDraft(current => current && { ...current, live: { ...current.live, ...change } });
  const route = `${endpoint}/${encodeURIComponent(name)}`;
  const inherited = scope === "workspace" && registry.origins?.[name] === "global";
  const locallySaved = scope === "global" || registry.origins?.[name] === "workspace";
  return <section className="settings-group" aria-label="Provider connections">
    <h3>Provider connections</h3>
    <p>{scope === "global" ? "Global connections are available to every workspace. Set a global default here." : "Add workspace connections or select a global connection for this workspace. Without a workspace selection, the global default applies."} Connections describe provider, credential source, endpoint and API. Choose the chat model independently in Agent profile and permissions.</p>
    <p>Configuration file: <code>{registry.path}</code></p>
    <p role="status">{notice || (registry.active ? `Active: ${registry.active}${scope === "workspace" && registry.inherited_active ? " (inherited global default)" : ""}` : "No named connection selected.")}</p>
    {error && <p role="alert" className="error-text">{error}</p>}
    <div className="settings-model-controls">
      <button type="button" disabled={busy} onClick={() => void refresh().catch(() => setError("Could not reload connections."))}>Reload connections</button>
      <button type="button" disabled={busy || blocked} onClick={() => {
        const kind = "openai";
        setName(""); setEditing(false); setDraft(defaultProfile(kind, registry.kinds));
      }}>Add connection</button>
    </div>
    <ul className="settings-model-results" aria-label="Saved provider connections">
      {Object.entries(registry.providers).map(([id, profile]) => <li key={id}>
        <button type="button" disabled={busy || blocked} onClick={() => { setName(id); setEditing(true); setDraft(editable(profile)); setError(""); }}>
          {id}{id === registry.active ? " (active)" : ""}{scope === "workspace" ? ` · ${registry.origins?.[id] === "workspace" ? "workspace" : "global"}` : ""} · {registry.kinds[profile.kind]?.label || profile.kind} · {profile.credential_source} · {profile.effective_endpoint}
        </button>
      </li>)}
    </ul>
    {draft && spec && <div className="settings-connection provider-editor">
      <h4>{editing ? `${inherited ? "Global connection" : "Edit"} ${name}` : `Add a ${scope} connection`}</h4>
      {inherited && <p>Edit this connection in Global settings. You can select it for this workspace here.</p>}
      <fieldset className="provider-fields" disabled={inherited || busy || blocked}>
      <label>Connection name <input value={name} disabled={editing} autoComplete="off" pattern="[a-z][a-z0-9_-]{0,63}" onChange={event => setName(event.target.value)} /></label>
       <label>Provider type <select value={draft.kind} onChange={event => edit(defaultProfile(event.target.value, registry.kinds))}>
        {Object.entries(registry.kinds).map(([id, kind]) => <option key={id} value={id}>{kind.label}</option>)}
      </select></label>
      <label>Authentication <select value={draft.auth} onChange={event => edit({ auth: event.target.value, api: draft.kind === "openai" && ["auto", "chatgpt", "codex"].includes(event.target.value) ? "auto" : draft.api, api_key_env: "" })}>
        {spec.auth.map(auth => <option key={auth} value={auth}>{auth === "auto" ? "Automatic (ChatGPT, Codex, then API key)" : auth === "codex" ? "Local Codex discovery" : auth === "chatgpt" ? "ngn ChatGPT device login" : auth === "entra" ? "Microsoft Entra ID (DefaultAzureCredential)" : "API key environment variable"}</option>)}
      </select></label>
      {(["api-key", "auto"].includes(draft.auth) || (draft.kind === "openai" && ["chatgpt", "codex"].includes(draft.auth))) && <label>API key environment variable
        <input value={draft.api_key_env} placeholder={spec.env} autoComplete="off" onChange={event => edit({ api_key_env: event.target.value })} />
         <small>Use NAME or {"${NAME}"}. No key value is saved. For ChatGPT/Codex this variable supplies a separate API key for Live. {editing && (registry.providers[name]?.key_configured ? "Variable is set on the server." : "Variable is not set on the server.")}</small>
       </label>}
      {!(draft.kind === "openai" && ["auto", "chatgpt", "codex"].includes(draft.auth)) && <label>API <select value={draft.api} onChange={event => edit({ api: event.target.value })}>{spec.apis.map(api => <option key={api} value={api}>{api}</option>)}</select></label>}
      {spec.endpoint_required && <label>API prefix URL <input value={draft.base_url} type="url" autoComplete="off" placeholder="https://resource.example.com/openai/v1" onChange={event => edit({ base_url: event.target.value })} /></label>}
      {spec.version_required && <label>API version <input value={draft.api_version} onChange={event => edit({ api_version: event.target.value })} /></label>}
      {draft.auth === "entra" && <label>Entra token scope <input value={draft.scope} onChange={event => edit({ scope: event.target.value })} /></label>}
      {spec.live && <details className="settings-disclosure"><summary>GPT-Live settings for this provider</summary>
        <label className="settings-checkbox"><input type="checkbox" checked={draft.live.enabled} onChange={event => editLive({ enabled: event.target.checked })} /> Enable Live</label>
        <label>Voice backend <select value={draft.live.backend_mode} onChange={event => editLive({ backend_mode: event.target.value })}><option value="assistant">Main assistant</option><option value="hosted">Separate hosted backend</option></select></label>
        <label>Voice model <input value={draft.live.model} onChange={event => editLive({ model: event.target.value })} /></label>
        <label>Hosted backend model <input value={draft.live.backend_model} onChange={event => editLive({ backend_model: event.target.value })} /></label>
        <label>Voice <select value={draft.live.voice} onChange={event => editLive({ voice: event.target.value })}><option value="marin">Marin</option><option value="cedar">Cedar</option></select></label>
      </details>}
      </fieldset>
      <div className="settings-model-controls">
        {!inherited && <button type="button" disabled={busy || blocked || !/^[a-z][a-z0-9_-]{0,63}$/.test(name) || (!editing && !!registry.providers[name])} onClick={() => void action(async () => (await (await request(route, token, { revision: registry.revision, profile: draft }, undefined, "PUT")).json()) as Registry, `Saved ${name} to ${scope} YAML.`)}>Save connection</button>}
        {editing && registry.providers[name] && <>
          <button type="button" disabled={busy || blocked || (name === registry.active && !registry.inherited_active)} onClick={() => void action(async () => (await (await request(`${route}/activate`, token, { revision: registry.revision })).json()) as Registry, `Using ${name} ${scope === "global" ? "globally" : "in this workspace"}.`)}>Make active</button>
          {locallySaved && <button type="button" disabled={busy || blocked || name === registry.active} onClick={() => void action(async () => (await (await request(route, token, { revision: registry.revision }, undefined, "DELETE")).json()) as Registry, `Deleted ${name}.`)}>Delete</button>}
        </>}
      </div>
    </div>}
    {scope === "workspace" && !registry.inherited_active && !!registry.global_active && <button type="button" disabled={busy || blocked} onClick={() => void action(async () => (await (await request("providers/inherit", token, { revision: registry.revision })).json()) as Registry, "Using the global default in this workspace.")}>Use global default</button>}
  </section>;
}
