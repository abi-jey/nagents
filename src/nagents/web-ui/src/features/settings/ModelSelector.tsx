import { useState } from "react";
import { request } from "../../api/client.js";
import type { SettingsScope } from "./transport.js";

export function ModelSelector({ token, scope, model, error, disabled, update }: {
  token: string; scope: SettingsScope; model: string; error?: string;
  disabled: boolean; update: (model: string) => void;
}) {
  const [models, setModels] = useState<string[]>([]);
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);

  async function discover() {
    setLoading(true); setNotice("");
    try {
      let endpoint = "models";
      if (scope === "global") {
        const registry = await (await request("provider-scopes/global/providers", token)).json() as { active: string };
        if (registry.active) endpoint = `providers/${encodeURIComponent(registry.active)}/models`;
      }
      const result = await (await request(endpoint, token)).json() as { models: string[] };
      setModels(result.models);
      setNotice(`${result.models.length} catalog IDs found. Choose one or enter an ID manually.`);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "Catalog unavailable; enter an ID manually.");
    } finally { setLoading(false); }
  }

  return <div className="settings-field">
    <label htmlFor="settings-model">Chat model (independent of provider connection)</label>
    <input id="settings-model" value={model} autoComplete="off" spellCheck={false}
      maxLength={200} aria-invalid={!!error} onChange={event => update(event.target.value)} />
    <p>Global/workspace model preference; an explicit agent model takes precedence.</p>
    {error && <p className="error-text">{error}</p>}
    <button type="button" disabled={disabled || loading} onClick={() => void discover()}>Browse model catalog</button>
    {notice && <p role="status">{notice}</p>}
    {!!models.length && <select aria-label="Catalog model IDs" value="" onChange={event => update(event.target.value)}>
      <option value="">Choose a discovered model…</option>
      {models.map(id => <option key={id} value={id}>{id}</option>)}
    </select>}
  </div>;
}
