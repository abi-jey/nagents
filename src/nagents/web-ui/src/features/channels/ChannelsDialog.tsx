import { useEffect, useRef, useState } from "react";
import type { Session } from "../../types.js";
import { hostProperty, installCommand, makeDraft, rootSessions, secretFields, simple, validSecretName } from "./draft.js";
import type { useChannels } from "./useChannels.js";
import type { Property } from "./types.js";
import { rememberDisclosure, revealInvalidField } from "../../components/disclosures.js";

function fieldLabel(key: string, field?: Property): string {
  return field?.title || key.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

export function ChannelsDialog({ channels, sessions, selected }: {
  channels: ReturnType<typeof useChannels>; sessions: Session[]; selected: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const feedback = useRef<HTMLDivElement>(null);
  const [packageName, setPackageName] = useState("nagents-channel-telegram-bot");
  const [copied, setCopied] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [secretName, setSecretName] = useState("");
  const [disclosures, setDisclosures] = useState(new Map<string, boolean>());
  const { catalog, draft, editing, pending, errors } = channels;
  const connection = catalog?.connections.find((item) => item.id === editing);
  const plugin = catalog?.plugins.find((item) => item.id === draft?.plugin);
  const secrets = [...new Set([...secretFields(plugin, connection), ...Object.keys(draft?.secrets || {})])]
    .filter((key) => !hostProperty(key, plugin?.schema.properties?.[key]));
  const properties = Object.entries(plugin?.schema.properties || {});
  const publicFields = properties.filter(([key, field]) => !hostProperty(key, field) && !secrets.includes(key) && simple(field));
  const requiredFields = publicFields.filter(([key]) => plugin?.schema.required?.includes(key));
  const optionalFields = publicFields.filter(([key]) => !plugin?.schema.required?.includes(key));
  const optionsKey = `channel-options:${draft?.plugin || ""}`;
  const jsonKey = `channel-json:${draft?.plugin || ""}`;
  const showJson = !properties.length || properties.some(([key, field]) =>
    !hostProperty(key, field) && !secrets.includes(key) && !simple(field) && plugin?.schema.required?.includes(key));
  const command = installCommand(catalog?.plugin_path || "", packageName);
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element?.showModal(); heading.current?.focus();
    return () => { element?.close(); if (previous instanceof HTMLElement) previous.focus({ preventScroll: true }); };
  }, []);
  useEffect(() => { if (channels.error || channels.notice) feedback.current?.focus(); }, [channels.error, channels.notice]);
  function fieldError(key: string) {
    return errors[key] ? <p id={`channel-${key}-error`} className="error-text">{errors[key]}</p> : null;
  }
  function toggle(key: string, open: boolean) {
    setDisclosures((current) => current.has(key) ? rememberDisclosure(current, key, open) : new Map(current).set(key, open));
  }
  function fieldHelp(key: string, field: Property = {}, secret = false) {
    return <details className="settings-help channel-field-help">
      <summary aria-label={`Details for ${fieldLabel(key, field)}`}>Field details</summary>
      <div id={`channel-${key}-help`}>
        <p><code>{key}</code>{field.description ? ` — ${field.description}` : ""}</p>
        {secret ? <p>Leave empty to keep the saved secret.{field.type && field.type !== "string" ? " Enter this property's value as JSON." : ""}</p>
          : field.type === "array" ? <p>Enter a JSON array.</p>
            : field.type === "string" && !field.enum && !plugin?.schema.required?.includes(key)
              ? <p>Leave empty to omit this property; Save removes its previous public value.</p> : null}
      </div>
    </details>;
  }
  function publicField([key, field]: [string, Property]) {
    if (!draft) return null;
    const numeric = field.type === "number" || field.type === "integer";
    const props = { id: `channel-${key}`, value: draft.fields[key] || "", "aria-invalid": !!errors[key],
      "aria-describedby": `channel-${key}-help${errors[key] ? ` channel-${key}-error` : ""}`,
      onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => channels.update({ fields: { ...draft.fields, [key]: event.target.value } }) };
    return <div className="settings-field" key={key}>
      <label htmlFor={props.id} title={key}>{fieldLabel(key, field)}{plugin?.schema.required?.includes(key) ? " *" : ""}</label>
      {field.enum ? <select {...props}><option value="">Use default / unset</option>{field.enum.map((item) => <option key={JSON.stringify(item)} value={JSON.stringify(item)}>{typeof item === "string" ? item : JSON.stringify(item)}</option>)}</select>
        : field.type === "boolean" ? <select {...props}><option value="">Use default / unset</option><option value="true">True</option><option value="false">False</option></select>
          : field.type === "array" ? <textarea {...props} rows={2} spellCheck={false} placeholder="[]" />
            : <input {...props} type={numeric ? "number" : "text"} step={numeric ? field.type === "integer" ? 1 : "any" : undefined}
                min={numeric ? field.minimum : undefined} max={numeric ? field.maximum : undefined} autoComplete="off" />}
      {fieldError(key)}{fieldHelp(key, field)}
    </div>;
  }
  return <dialog ref={dialog} className="settings-dialog channels-dialog" aria-labelledby="channels-title" aria-describedby="channels-description"
    onCancel={(event) => { event.preventDefault(); channels.close(); }}>
    <header className="settings-heading">
      <h2 id="channels-title" ref={heading} tabIndex={-1}>Channels</h2>
      <p id="channels-description">Connect installed plugins. Each chat gets a session; /session main attaches it to your chosen main session.</p>
    </header>
    <form noValidate onSubmit={async (event) => {
      event.preventDefault(); if (deleting) return;
      await channels.save();
      requestAnimationFrame(() => { if (dialog.current) revealInvalidField(dialog.current, toggle); });
    }}>
      <div className="settings-body" aria-busy={pending}>
        <div className="settings-feedback" ref={feedback} tabIndex={-1}>
          <div role="status" className="settings-status">{pending ? "Applying channel request; waiting for an idle boundary if needed…" : channels.notice}</div>
          {channels.error && <p className="error-text" role="alert">{channels.error}</p>}
          {Object.values(errors).some(Boolean) && <p className="error-text" role="alert">Check the highlighted fields before saving.</p>}
        </div>
        <div className="settings-refresh"><button type="button" disabled={pending} onClick={() => void channels.refresh()}>Refresh installed plugins</button>
          <span>Discovers newly installed entry points; keeps your draft.</span></div>
        {catalog && <>
          <details className="settings-disclosure channel-install">
            <summary>Install a plugin</summary>
            <p>Plugin directory</p><code className="channel-path">{catalog.plugin_path}</code>
            <div className="settings-field"><label htmlFor="channel-package">Package name (optional ==version)</label>
              <input id="channel-package" value={packageName} autoComplete="off" spellCheck={false} onChange={(event) => { setPackageName(event.target.value); setCopied(""); }} />
            </div>
            {command ? <><pre className="channel-command" tabIndex={0}>{command}</pre>
              <button type="button" onClick={async () => {
                try { await navigator.clipboard.writeText(command); setCopied("Command copied."); }
                catch { setCopied("Copy unavailable. Select and copy the command above."); }
              }}>Copy install command</button></> : <p>Enter a distribution name or an exact version to generate the command.</p>}
            <p role="status">{copied}</p>
            <p>Ask the agent to run this command through its approved shell, then Refresh. Install dependencies explicitly when needed (--no-deps). Upgrading already imported packages may require a server restart.</p>
          </details>
          <fieldset className="settings-group" disabled={pending}>
            <legend>Connections</legend>
            <div className="settings-field"><label htmlFor="channel-instance">Saved instance</label>
              <select id="channel-instance" value={editing} onChange={(event) => { channels.choose(event.target.value); setDeleting(false); setSecretName(""); }}>
                <option value="">Add channel…</option>
                {catalog.connections.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.enabled ? item.status : "Disabled"}</option>)}
              </select>
              <p>Choosing an instance replaces the current unsaved draft.</p>
            </div>
            {connection && <div className="channel-state">
              <p>Status: <strong>{connection.enabled ? connection.status : "Disabled"}</strong></p>
              {connection.error && <p className="error-text" role="status">Connection needs attention. Check the plugin configuration and credentials, then Save or disable this instance.</p>}
              <details className="settings-help"><summary>Saved configuration and chat bindings</summary>
                <pre className="channel-command">{JSON.stringify({
                  plugin: connection.plugin, enabled: connection.enabled, main_session_id: connection.main_session_id,
                  config: Object.fromEntries(Object.entries(connection.config).filter(([key]) => !secrets.includes(key))),
                  configured_secrets: connection.configured_secrets,
                }, null, 2)}</pre>
                <ul>{catalog.bindings.filter((item) => item.channel === connection.id).map((item) =>
                  <li key={item.conversation_id}>{item.conversation_id} → {sessions.find((session) => session.id === item.session_id)?.title || item.session_id}</li>)}</ul>
              </details>
            </div>}
          </fieldset>
          {draft && <fieldset className="settings-group" disabled={pending || deleting}>
            <legend>{editing ? "Edit channel" : "Add channel"}</legend>
            <div className="channel-grid">
              <div className="settings-field"><label htmlFor="channel-plugin">Installed plugin</label>
                <select id="channel-plugin" value={draft.plugin} disabled={!!editing} aria-invalid={!!errors.plugin} aria-describedby="channel-plugin-error"
                  onChange={(event) => channels.update({ ...makeDraft(catalog, draft.mainSessionId || selected, undefined, event.target.value), id: draft.id, enabled: draft.enabled })}>
                  {!plugin && <option value={draft.plugin}>{draft.plugin || "No plugins installed"}</option>}
                  {catalog.plugins.map((item) => <option key={item.id} value={item.id}>{item.name}{item.version ? ` · ${item.version}` : ""}</option>)}
                </select>{fieldError("plugin")}
              </div>
              <div className="settings-field"><label htmlFor="channel-id">Stable instance ID</label>
                <input id="channel-id" value={draft.id} readOnly={!!editing} autoComplete="off" spellCheck={false} required
                  aria-invalid={!!errors.id} aria-describedby="channel-id-help channel-id-error" onChange={(event) => channels.update({ id: event.target.value })} />
                <p id="channel-id-help">Fixed routing ID after Save.</p>{fieldError("id")}
              </div>
            </div>
            {plugin?.description && <details className="settings-help"><summary>Plugin details</summary><p>{plugin.description}</p></details>}
            <div className="settings-field"><label className="settings-checkbox"><input type="checkbox" checked={draft.enabled} onChange={(event) => channels.update({ enabled: event.target.checked })} />Enabled</label></div>
            <div className="settings-field"><label htmlFor="channel-mainSessionId">Main session</label>
              <select id="channel-mainSessionId" value={draft.mainSessionId} required aria-invalid={!!errors.mainSessionId} aria-describedby="channel-main-help channel-mainSessionId-error"
                onChange={(event) => channels.update({ mainSessionId: event.target.value })}>
                {!rootSessions(sessions).some((item) => item.id === draft.mainSessionId) && <option value={draft.mainSessionId}>Choose a root session</option>}
                {rootSessions(sessions).map((item) => <option key={item.id} value={item.id}>{item.title} · {item.id}</option>)}
              </select><p id="channel-main-help">Saved target for /session main.</p>{fieldError("mainSessionId")}
            </div>
            {requiredFields.map(publicField)}
            {secrets.map((key) => <div className="settings-field" key={key}>
              <label htmlFor={`channel-${key}`} title={key}>{fieldLabel(key, plugin?.schema.properties?.[key])} <span className="channel-secret-state">{connection?.configured_secrets.includes(key) ? "Configured" : "Not configured"}</span></label>
              <input id={`channel-${key}`} type="password" autoComplete="new-password" spellCheck={false} value={draft.secrets[key] || ""} disabled={draft.clear.includes(key)}
                placeholder={connection?.configured_secrets.includes(key) ? "Leave blank to keep saved secret" : undefined}
                aria-invalid={!!errors[key]} aria-describedby={`channel-${key}-help channel-${key}-error`}
                onChange={(event) => channels.update({ secrets: { ...draft.secrets, [key]: event.target.value } })} />
              {fieldError(key)}
              {connection?.configured_secrets.includes(key) && <label className="settings-checkbox"><input type="checkbox" checked={draft.clear.includes(key)} aria-invalid={!!errors[key]}
                onChange={(event) => channels.update({ clear: event.target.checked ? [...draft.clear, key] : draft.clear.filter((item) => item !== key) })} />Clear saved {key} on Save</label>}
              {fieldHelp(key, plugin?.schema.properties?.[key], true)}
            </div>)}
            {!!optionalFields.length && <details className="settings-disclosure channel-options" data-disclosure-key={optionsKey}
              open={disclosures.get(optionsKey) ?? false} onToggle={(event) => toggle(optionsKey, event.currentTarget.open)}>
              <summary>More connection options</summary>
              {optionalFields.map(publicField)}
            </details>}
            <details className="settings-help">
              <summary>Add a secret field</summary>
              <div className="settings-field"><label htmlFor="channel-secret-name">Secret property name</label>
                <div className="settings-model-controls"><input id="channel-secret-name" value={secretName} autoComplete="off" spellCheck={false} onChange={(event) => setSecretName(event.target.value)} />
                  <button type="button" disabled={!validSecretName(secretName) || secrets.includes(secretName) || hostProperty(secretName, plugin?.schema.properties?.[secretName])} onClick={() => {
                    channels.update({ secrets: { ...draft.secrets, [secretName]: "" } }); setSecretName("");
                  }}>Add password input</button></div>
                <p>For plain factories without a schema. Refresh installed plugins to load available descriptor fields.</p>
              </div>
            </details>
            <details className="settings-disclosure" data-disclosure-key={jsonKey} open={disclosures.get(jsonKey) ?? showJson}
              onToggle={(event) => toggle(jsonKey, event.currentTarget.open)}>
              <summary>Additional configuration (JSON)</summary>
              <div className="settings-field"><label htmlFor="channel-json">Public JSON configuration</label>
                <textarea id="channel-json" rows={5} spellCheck={false} value={draft.json} aria-invalid={!!errors.json} aria-describedby="channel-json-help channel-json-error" onChange={(event) => channels.update({ json: event.target.value })} />
                <p id="channel-json-help">For nested or plugin-specific properties. Secret fields belong in password inputs.</p>{fieldError("json")}
              </div>
            </details>
          </fieldset>}
          {editing && <div className="settings-refresh">
            {deleting ? <><span>Delete {editing}?</span><button type="button" disabled={pending || channels.conflict} onClick={async () => { await channels.remove(); setDeleting(false); }}>Confirm delete</button><button type="button" disabled={pending} onClick={() => setDeleting(false)}>Keep instance</button></>
              : <button type="button" disabled={pending || channels.conflict} onClick={() => setDeleting(true)}>Delete instance</button>}
          </div>}
        </>}
      </div>
      <footer className="settings-actions"><span>{channels.conflict ? "Refresh required; draft preserved" : "Changes apply only on Save"}</span>
        <button type="button" disabled={pending} onClick={channels.close}>Close</button>
        <button className="primary" type="submit" disabled={pending || !draft || !plugin || channels.conflict || deleting}>Save</button>
      </footer>
    </form>
  </dialog>;
}
