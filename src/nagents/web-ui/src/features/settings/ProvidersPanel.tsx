import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { request } from "../../api/client.js";
import { ProviderLogo } from "../../components/ProviderLogo.js";
import { Icon } from "../../components/Icon.js";
import type { SettingsScope } from "./transport.js";

type Profile = {
  kind: string; auth: string; base_url: string; api: string;
  api_key_env: string; api_version: string; scope: string;
  request_timeout?: number;
  context_window?: number;
  model_context_windows?: Record<string, number>;
  key_configured?: boolean; credential_source?: string; effective_endpoint?: string;
};
type WindowDraft = { model: string; tokens: string };
type ProfileDraft = Omit<Profile, "request_timeout" | "context_window" | "model_context_windows"> & {
  request_timeout: string; context_window: string; model_context_windows: WindowDraft[];
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

export type ProviderSelection = {
  id: string;
  revision: string;
  active: string;
  kind: string;
  label: string;
  auth: string;
  inherited: boolean;
  contextWindow: number;
  modelContextWindows: Record<string, number>;
};

export type ProviderWorkspace = ProviderSelection & {
  configure: () => void;
  activate: () => void;
  busy: boolean;
};

function defaultProfile(kind: string, specs: Record<string, Kind>): ProfileDraft {
  return {
    kind, auth: specs[kind].auth[0], base_url: "", api: specs[kind].apis[0],
    api_key_env: "", api_version: "", scope: "https://ai.azure.com/.default",
    request_timeout: "120",
    context_window: "", model_context_windows: [],
  };
}

function editable(profile: Profile): ProfileDraft {
  return { kind: profile.kind, auth: profile.auth, base_url: profile.base_url,
    api: profile.api, api_key_env: profile.api_key_env, api_version: profile.api_version,
    scope: profile.scope, request_timeout: String(profile.request_timeout ?? 120),
    context_window: profile.context_window ? String(profile.context_window) : "",
    model_context_windows: Object.entries(profile.model_context_windows || {}).map(([model, tokens]) => ({ model, tokens: String(tokens) })) };
}

function validWindows(rows: WindowDraft[]): boolean {
  const names = rows.map(row => row.model.trim());
  return rows.length <= 1024 && new Set(names).size === rows.length && rows.every(row => row.model.trim().length > 0 && row.model.trim().length <= 200 && Number.isSafeInteger(Number(row.tokens)) && Number(row.tokens) > 0 && Number(row.tokens) <= 1_000_000_000);
}

export function ProvidersPanel({ token, blocked, applied, scope, openGlobal, onDraftChange, onBusyChange,
  children, onSelectionChange, confirmActivation, login, loginConnection = "",
}: {
  token: string; blocked: boolean; applied: () => void | Promise<void>; scope: SettingsScope;
  openGlobal?: () => void; onDraftChange?: (dirty: boolean) => void; onBusyChange?: (busy: boolean) => void;
  children?: (selection: ProviderWorkspace) => ReactNode;
  onSelectionChange?: (selection: ProviderSelection) => void;
  confirmActivation?: (apply: () => Promise<void>) => void;
  login?: () => void;
  loginConnection?: string;
}) {
  const [registry, setRegistry] = useState<Registry>();
  const [name, setName] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<ProfileDraft>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState("");
  const [switchEditor, setSwitchEditor] = useState<(() => void)>();
  const reading = useRef<AbortController>(undefined);
  const timeoutHelpId = useId();
  const timeoutErrorId = useId();
  const contextHelpId = useId();

  const endpoint = `provider-scopes/${scope}/providers`;
  async function refresh() {
    reading.current?.abort();
    const controller = new AbortController(); reading.current = controller;
    const value = (await (await request(endpoint, token, undefined, controller.signal)).json()) as Registry;
    if (controller.signal.aborted || reading.current !== controller) return;
    setRegistry(value);
    setSelected(current => value.providers[current] ? current : value.active);
    setError("");
    reading.current = undefined;
  }
  useEffect(() => {
    setRegistry(undefined); setDraft(undefined); setName(""); setSelected("");
    void refresh().catch(() => setError("Could not load provider connections."));
    return () => { reading.current?.abort(); };
  }, [token, scope]);
  const inherited = scope === "workspace" && registry?.origins?.[name] === "global";
  const connectionDirty = !!registry && !!draft && !inherited &&
    (!editing || !registry.providers[name] || JSON.stringify(draft) !== JSON.stringify(editable(registry.providers[name])));
  useEffect(() => { onDraftChange?.(connectionDirty); }, [connectionDirty, onDraftChange]);
  useEffect(() => () => onDraftChange?.(false), [onDraftChange]);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);

  const selectedProfile = registry?.providers[selected];
  const selectedKind = selectedProfile?.kind || "";
  const selection: ProviderSelection = {
    // An inherited global profile can change without changing the local registry revision.
    id: selected, revision: `${registry?.revision || ""}:${JSON.stringify(selectedProfile || {})}`, active: registry?.active || "", kind: selectedKind,
    label: registry?.kinds[selectedKind]?.label || selectedKind,
    auth: selectedProfile?.auth || "", inherited: scope === "workspace" && !!registry?.inherited_active,
    contextWindow: selectedProfile?.context_window || 0,
    modelContextWindows: selectedProfile?.model_context_windows || {},
  };
  useEffect(() => { if (registry) onSelectionChange?.(selection); }, [registry, selected, onSelectionChange]);

  async function action(run: () => Promise<Registry>, message: string, saved = false) {
    if (busy || blocked) return;
    reading.current?.abort(); setBusy(true); setError(""); setNotice("");
    try {
      const next = await run();
      setRegistry(next);
      setSelected(current => next.providers[current] ? current : next.active);
      if (saved) { setDraft(editable(next.providers[name])); setEditing(true); setSelected(name); }
      else if (draft && !next.providers[name]) { setDraft(undefined); setName(""); }
      setNotice(message);
      if (next.active !== registry?.active || (editing && next.active === name)) await applied();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Provider operation failed; reload connections.");
    } finally { setBusy(false); }
  }
  if (!registry) return <div className={children ? "settings-workspace-grid" : undefined}><section className={children ? "settings-provider-rail" : "settings-group"} aria-label="Provider connections"><h3>Provider connections</h3><p role="status">{error || "Loading connections…"}</p>{error && <button type="button" onClick={() => void refresh().catch(() => setError("Could not load provider connections."))}>Retry connections</button>}</section>{children && <div className="settings-model-workspace">{children({ ...selection, busy: true, configure: () => {}, activate: () => {} })}</div>}</div>;
  const spec = draft ? registry.kinds[draft.kind] : undefined;
  const edit = (change: Partial<ProfileDraft>) => setDraft(current => current && { ...current, ...change });
  const timeout = Number(draft?.request_timeout);
  const timeoutValid = Number.isFinite(timeout) && timeout > 0;
  const contextWindow = Number(draft?.context_window || 0);
  const connectionWindowValid = Number.isSafeInteger(contextWindow) && contextWindow >= 0 && contextWindow <= 1_000_000_000;
  const contextValid = connectionWindowValid && validWindows(draft?.model_context_windows || []);
  const route = `${endpoint}/${encodeURIComponent(name)}`;
  const locallySaved = scope === "global" || registry.origins?.[name] === "workspace";
  function changeEditor(next: () => void) {
    if (connectionDirty) setSwitchEditor(() => next);
    else next();
  }
  function configure(id: string) {
    changeEditor(() => { setName(id); setEditing(true); setDraft(editable(registry!.providers[id])); setError(""); });
  }
  function activate(id: string) {
    const apply = () => action(async () => (await (await request(`${endpoint}/${encodeURIComponent(id)}/activate`, token, { revision: registry!.revision })).json()) as Registry,
      `Using ${id} ${scope === "global" ? "globally" : "in this workspace"}.`);
    if (confirmActivation) confirmActivation(apply);
    else void apply();
  }
  const inherit = () => {
    const apply = () => action(async () => (await (await request("providers/inherit", token, { revision: registry.revision })).json()) as Registry, "Using the global default in this workspace.");
    if (confirmActivation) confirmActivation(apply);
    else void apply();
  };
  return <div className={children ? "settings-workspace-grid" : undefined}>
    <section className={children ? "settings-provider-rail" : "settings-group"} aria-label="Provider connections">
    <div className="provider-rail-heading"><h3>Provider connections</h3><button type="button" title={connectionDirty ? "Save or discard connection edits before reloading" : "Reload connections"} aria-label="Reload connections" disabled={busy || connectionDirty} onClick={() => void refresh().catch(() => setError("Could not reload connections."))}>↻</button></div>
    <p className="provider-rail-help">{scope === "global" ? "Global connections are available to every workspace." : "Choose a connection to browse its models."}</p>
    <p className="provider-rail-status" role="status">{notice || (registry.active ? `Active: ${registry.active}${scope === "workspace" && registry.inherited_active ? " · from Global" : ""}` : "Using startup configuration.")}</p>
    {error && <p role="alert" className="error-text">{error}</p>}
    <ul className="provider-connection-list" aria-label="Saved provider connections">
      {Object.entries(registry.providers).map(([id, profile]) => <li key={id} className={selected === id ? "is-selected" : ""}>
        <button type="button" className="provider-connection-select" aria-pressed={selected === id} disabled={busy || blocked} onClick={() => {
          if (children) changeEditor(() => { setSelected(id); setDraft(undefined); setName(""); });
          else { setSelected(id); configure(id); }
        }}>
          <ProviderLogo provider={profile.kind} /><span><strong>{id}<span className="sr-only">{id === registry.active ? " (active)" : ""}</span></strong>
            <small>{registry.kinds[profile.kind]?.label || profile.kind} · {profile.auth === "chatgpt" || profile.auth === "codex" ? "ChatGPT / Codex" : profile.auth === "entra" ? "Entra ID" : profile.auth === "auto" ? "Automatic" : "API key"}</small>
            <span className="provider-row-status">{id === registry.active ? "Active" : scope === "workspace" && registry.origins?.[id] === "global" ? "Global connection" : "Saved connection"}</span>
          </span>
        </button>
        {children && <button type="button" className="provider-connection-edit" aria-label={`Edit ${id} connection`} title={`Edit ${id}`} disabled={busy || blocked} onClick={() => configure(id)}><Icon name="settings" size={14} /></button>}
      </li>)}
    </ul>
    <button type="button" className="provider-add" disabled={busy || blocked} onClick={() => changeEditor(() => {
        const kind = "openai";
        setName(""); setEditing(false); setDraft(defaultProfile(kind, registry.kinds));
      })}><Icon name="plus" size={15} />Add connection</button>
    {scope === "workspace" && !registry.inherited_active && !!registry.global_active && <button className="provider-inherit" type="button" disabled={busy || blocked} onClick={inherit}>Use global connection</button>}
    <details className="provider-storage"><summary>Connection storage</summary><p>Configuration file: <code>{registry.path}</code></p><p>Connection changes apply immediately. Model settings are saved separately.</p></details>
    </section>
    <div className={children ? "settings-model-workspace" : undefined}>
    {switchEditor && <section className="settings-confirmation" role="alert"><h3>Discard connection edits?</h3><p>Your connection has unsaved changes.</p><div className="settings-confirmation-actions"><button type="button" onClick={() => { switchEditor(); setSwitchEditor(undefined); }}>Discard connection edits</button><button type="button" onClick={() => setSwitchEditor(undefined)}>Keep editing connection</button></div></section>}
    {!draft && children?.({ ...selection, busy: busy || blocked, configure: () => { if (selected) configure(selected); }, activate: () => activate(selected) })}
    {draft && spec && <div className="settings-connection provider-editor">
      {children && <button type="button" className="provider-back" disabled={busy} onClick={() => changeEditor(() => setDraft(undefined))}>← Back to model settings</button>}
      <h4>{editing ? `${inherited ? "Global connection" : "Edit"} ${name}` : `Add a ${scope} connection`}</h4>
      {editing && <p>{registry.providers[name]?.credential_source}<br />{registry.providers[name]?.effective_endpoint}</p>}
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
      <details className="provider-context-limits">
        <summary>Context window overrides</summary>
        <p id={contextHelpId}>Use the provider&apos;s advertised input limit. Exact model overrides take priority over the connection limit; leave both unset to use discovered metadata. Unknown limits fall back to a labelled guess.</p>
        <label>Connection input limit (tokens)
          <input type="text" inputMode="numeric" placeholder="Automatic" value={draft.context_window} aria-invalid={!connectionWindowValid} aria-describedby={contextHelpId}
            onInput={event => edit({ context_window: event.currentTarget.value })} />
          <small>Applies to every model on this connection unless overridden below. Empty or 0 means automatic.</small>
        </label>
        {draft.model_context_windows.map((row, index) => <div className="provider-model-limit" key={index}>
          <label>Model ID<input value={row.model} maxLength={200} spellCheck={false} autoComplete="off" placeholder="Exact model ID"
            onInput={event => edit({ model_context_windows: draft.model_context_windows.map((item, i) => i === index ? { ...item, model: event.currentTarget.value } : item) })} /></label>
          <label>Input limit (tokens)<input type="number" min="1" max="1000000000" step="1" value={row.tokens}
            onInput={event => edit({ model_context_windows: draft.model_context_windows.map((item, i) => i === index ? { ...item, tokens: event.currentTarget.value } : item) })} /></label>
          <button type="button" aria-label={`Remove context limit for ${row.model || "model"}`} onClick={() => edit({ model_context_windows: draft.model_context_windows.filter((_, i) => i !== index) })}>Remove</button>
        </div>)}
        <button type="button" disabled={draft.model_context_windows.length >= 1024} onClick={() => edit({ model_context_windows: [...draft.model_context_windows, { model: "", tokens: "" }] })}>Add model limit</button>
        {!contextValid && <p className="error-text" role="alert">Use whole tokens up to 1,000,000,000, and a unique model ID with a positive limit for each override. Only the connection limit may be empty or 0.</p>}
      </details>
      </fieldset>
      {login && scope === "workspace" && editing && name === registry.active && name === loginConnection && !connectionDirty && draft.kind === "openai" && ["auto", "chatgpt", "codex"].includes(draft.auth) && <div className="provider-login"><p>Sign in for the active workspace connection, {name}. Credentials stay on the ngn server.</p><button type="button" disabled={busy || blocked} onClick={login}>Sign in with ChatGPT</button></div>}
      <div className="settings-model-controls">
        {!inherited && <button type="button" disabled={busy || blocked || !timeoutValid || !contextValid || !/^[a-z][a-z0-9_-]{0,63}$/.test(name) || (!editing && !!registry.providers[name])} onClick={() => void action(async () => (await (await request(route, token, { revision: registry.revision, profile: { ...draft, request_timeout: timeout, context_window: contextWindow,
          model_context_windows: Object.fromEntries(draft.model_context_windows.map(row => [row.model.trim(), Number(row.tokens)])) } }, undefined, "PUT")).json()) as Registry, `Saved ${name} to ${scope} settings.`, true)}>Save connection</button>}
        {editing && registry.providers[name] && <>
          <button type="button" disabled={busy || blocked || (name === registry.active && !registry.inherited_active) || connectionDirty} onClick={() => activate(name)}>Make active</button>
          {locallySaved && <button type="button" disabled={busy || blocked || name === registry.active} onClick={() => void action(async () => (await (await request(route, token, { revision: registry.revision }, undefined, "DELETE")).json()) as Registry, `Deleted ${name}.`)}>Delete</button>}
        </>}
      </div>
    </div>}
    </div>
  </div>;
}
