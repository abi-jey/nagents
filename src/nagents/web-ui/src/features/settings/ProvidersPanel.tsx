import { useEffect, useId, useState } from "react";
import { request } from "../../api/client.js";
import type { SettingsScope } from "./transport.js";

type Profile = {
  kind: string; auth: string; base_url: string; api: string;
  api_key_env: string; api_version: string; scope: string;
  request_timeout?: number;
  key_configured?: boolean; credential_source?: string; effective_endpoint?: string;
};
type ProfileDraft = Omit<Profile, "request_timeout"> & { request_timeout: string };
type Kind = {
  label: string; auth: string[]; apis: string[]; env: string;
  endpoint_required: boolean; version_required: boolean; live: boolean;
};
type Registry = {
  revision: string; active: string; providers: Record<string, Profile>; kinds: Record<string, Kind>;
  scope: SettingsScope; path: string; origins?: Record<string, SettingsScope>;
  inherited_active?: boolean; global_active?: string;
};

function defaultProfile(kind: string, specs: Record<string, Kind>): ProfileDraft {
  return {
    kind, auth: specs[kind].auth[0], base_url: "", api: specs[kind].apis[0],
    api_key_env: "", api_version: "", scope: "https://ai.azure.com/.default",
    request_timeout: "120",
  };
}

function editable(profile: Profile): ProfileDraft {
  return { kind: profile.kind, auth: profile.auth, base_url: profile.base_url,
    api: profile.api, api_key_env: profile.api_key_env, api_version: profile.api_version,
    scope: profile.scope, request_timeout: String(profile.request_timeout ?? 120) };
}

export function ProvidersPanel({ token, blocked, applied, scope, openGlobal, onDraftChange, onBusyChange }: {
  token: string; blocked: boolean; applied: () => void; scope: SettingsScope;
  openGlobal?: () => void; onDraftChange?: (dirty: boolean) => void; onBusyChange?: (busy: boolean) => void;
}) {
  const [registry, setRegistry] = useState<Registry>();
  const [name, setName] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<ProfileDraft>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const timeoutHelpId = useId();
  const timeoutErrorId = useId();

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
  const inherited = scope === "workspace" && registry?.origins?.[name] === "global";
  const connectionDirty = !!registry && !!draft && !inherited &&
    (!editing || !registry.providers[name] || JSON.stringify(draft) !== JSON.stringify(editable(registry.providers[name])));
  useEffect(() => { onDraftChange?.(connectionDirty); }, [connectionDirty, onDraftChange]);
  useEffect(() => () => onDraftChange?.(false), [onDraftChange]);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);

  async function action(run: () => Promise<Registry>, message: string, saved = false) {
    if (busy || blocked) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const next = await run();
      setRegistry(next);
      if (saved) { setDraft(editable(next.providers[name])); setEditing(true); }
      setNotice(message);
      if (next.active !== registry?.active || (editing && next.active === name)) applied();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Provider operation failed; reload connections.");
    } finally { setBusy(false); }
  }
  if (!registry) return <section className="settings-group" aria-label="Provider connections"><h3>Provider connections</h3><p role="status">{error || "Loading connections…"}</p></section>;
  const spec = draft ? registry.kinds[draft.kind] : undefined;
  const edit = (change: Partial<ProfileDraft>) => setDraft(current => current && { ...current, ...change });
  const timeout = Number(draft?.request_timeout);
  const timeoutValid = Number.isFinite(timeout) && timeout > 0;
  const route = `${endpoint}/${encodeURIComponent(name)}`;
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
      {inherited && <><p>Edit this connection in Global settings. You can select it for this workspace here.</p>
        {openGlobal && <button type="button" disabled={busy || blocked} onClick={openGlobal}>Switch to Global settings</button>}</>}
      <fieldset className="provider-fields" disabled={inherited || busy || blocked}>
      <label>Connection name <input value={name} disabled={editing} autoComplete="off" pattern="[a-z][a-z0-9_-]{0,63}" onChange={event => setName(event.target.value)} /></label>
       <label>Provider type <select value={draft.kind} onChange={event => edit({ ...defaultProfile(event.target.value, registry.kinds), request_timeout: draft.request_timeout })}>
        {Object.entries(registry.kinds).map(([id, kind]) => <option key={id} value={id}>{kind.label}</option>)}
      </select></label>
      <label>Authentication <select value={draft.auth} onChange={event => edit({ auth: event.target.value, api: draft.kind === "openai" && ["auto", "chatgpt", "codex"].includes(event.target.value) ? "auto" : draft.api, api_key_env: "" })}>
        {spec.auth.map(auth => <option key={auth} value={auth}>{auth === "auto" ? "Automatic (ChatGPT, Codex, then API key)" : auth === "codex" ? "Local Codex discovery" : auth === "chatgpt" ? "ngn ChatGPT device login" : auth === "entra" ? "Microsoft Entra ID (DefaultAzureCredential)" : "API key environment variable"}</option>)}
      </select></label>
      {(["api-key", "auto"].includes(draft.auth) || (draft.kind === "openai" && ["chatgpt", "codex"].includes(draft.auth))) && <label>API key environment variable
        <input value={draft.api_key_env} placeholder={spec.env} autoComplete="off" onChange={event => edit({ api_key_env: event.target.value })} />
         <small>Use NAME or {"${NAME}"}. No key value is saved. {editing && (registry.providers[name]?.key_configured ? "Variable is set on the server." : "Variable is not set on the server.")}</small>
       </label>}
      {!(draft.kind === "openai" && ["auto", "chatgpt", "codex"].includes(draft.auth)) && <label>API <select value={draft.api} onChange={event => edit({ api: event.target.value })}>{spec.apis.map(api => <option key={api} value={api}>{api}</option>)}</select></label>}
      {spec.endpoint_required && <label>API prefix URL <input value={draft.base_url} type="url" autoComplete="off" placeholder="https://resource.example.com/openai/v1" onChange={event => edit({ base_url: event.target.value })} /></label>}
      {spec.version_required && <label>API version <input value={draft.api_version} onChange={event => edit({ api_version: event.target.value })} /></label>}
      {draft.auth === "entra" && <label>Entra token scope <input value={draft.scope} onChange={event => edit({ scope: event.target.value })} /></label>}
      <label>Request timeout (seconds)
        <input type="number" min="0" step="any" required value={draft.request_timeout}
          aria-invalid={!timeoutValid} aria-describedby={`${timeoutHelpId}${timeoutValid ? "" : ` ${timeoutErrorId}`}`}
          onInput={event => edit({ request_timeout: event.currentTarget.value })} />
        <small id={timeoutHelpId}>Deadline for each model HTTP request, including ChatGPT/Codex. Default: 120 seconds. Shell commands use their own timeout.</small>
        {!timeoutValid && <small id={timeoutErrorId} className="error-text" role="alert">Enter a finite number of seconds greater than zero.</small>}
      </label>
      </fieldset>
      <div className="settings-model-controls">
        {!inherited && <button type="button" disabled={busy || blocked || !timeoutValid || !/^[a-z][a-z0-9_-]{0,63}$/.test(name) || (!editing && !!registry.providers[name])} onClick={() => void action(async () => (await (await request(route, token, { revision: registry.revision, profile: { ...draft, request_timeout: timeout } }, undefined, "PUT")).json()) as Registry, `Saved ${name} to ${scope} YAML.`, true)}>Save connection</button>}
        {editing && registry.providers[name] && <>
          <button type="button" disabled={busy || blocked || (name === registry.active && !registry.inherited_active)} onClick={() => void action(async () => (await (await request(`${route}/activate`, token, { revision: registry.revision })).json()) as Registry, `Using ${name} ${scope === "global" ? "globally" : "in this workspace"}.`)}>Make active</button>
          {locallySaved && <button type="button" disabled={busy || blocked || name === registry.active} onClick={() => void action(async () => (await (await request(route, token, { revision: registry.revision }, undefined, "DELETE")).json()) as Registry, `Deleted ${name}.`)}>Delete</button>}
        </>}
      </div>
    </div>}
    {scope === "workspace" && !registry.inherited_active && !!registry.global_active && <button type="button" disabled={busy || blocked} onClick={() => void action(async () => (await (await request("providers/inherit", token, { revision: registry.revision })).json()) as Registry, "Using the global default in this workspace.")}>Use global default</button>}
  </section>;
}
