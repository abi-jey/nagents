import { useEffect, useState } from "react";
import { request } from "../../api/client.js";
import { ModelSelector } from "./ModelSelector.js";
import type { ProviderRegistry } from "./ProvidersPanel.js";
import type { ModelBinding } from "./types.js";

/** A model always travels with the saved connection used to discover it. */
export function ModelBindingPicker({ token, value, change, disabled = false, named = false }: {
  token: string; value: ModelBinding; change: (binding: ModelBinding) => void; disabled?: boolean; named?: boolean;
}) {
  const [registry, setRegistry] = useState<ProviderRegistry>();
  const [error, setError] = useState("");
  const [revision, refresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void request("provider-scopes/workspace/providers", token, undefined, controller.signal)
      .then(response => response.json()).then((reply: ProviderRegistry) => { if (!controller.signal.aborted) { setRegistry(reply); setError(""); } })
      .catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not load connections."); });
    return () => controller.abort();
  }, [token, revision]);
  const unavailable = !!registry && !!value.provider && !registry.providers[value.provider];
  return <div className="model-binding-picker">
    <label>Provider connection<select value={value.provider} disabled={disabled || !registry}
      onChange={event => change({ provider: event.target.value, id: "" })}>
      {!value.provider && <option value="" disabled={named}>{named ? "Choose a saved connection" : "Startup connection"}</option>}
      {unavailable && <option value={value.provider} disabled>{value.provider} · unavailable</option>}
      {Object.entries(registry?.providers || {}).map(([id, profile]) => <option key={id} value={id}>
        {id} · {registry?.kinds[profile.kind]?.label || profile.kind} · {registry?.origins?.[id] === "global" ? "Global" : "Workspace"}
      </option>)}
    </select></label>
    {error && <p className="error-text" role="alert">{error} <button type="button" disabled={disabled} onClick={() => refresh(value => value + 1)}>Reload connections</button></p>}
    {registry && named && !Object.keys(registry.providers).length && <p className="settings-origin">Add a saved provider connection in Settings to choose a model for this agent.</p>}
    {unavailable && <p className="error-text" role="status">This saved connection is unavailable in this workspace. Choose another connection.</p>}
    <ModelSelector token={token} scope="workspace" provider={value.provider} revision={registry?.revision || ""}
      model={value.id} update={id => change({ provider: value.provider, id })}
      disabled={disabled || !registry || unavailable || (named && !value.provider)} autoLoad />
  </div>;
}
