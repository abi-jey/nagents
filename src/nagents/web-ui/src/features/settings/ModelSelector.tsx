import { useEffect, useRef, useState } from "react";
import { request } from "../../api/client.js";
import { Icon } from "../../components/Icon.js";
import type { SettingsScope } from "./transport.js";

export type ModelMetadata = { context_window?: number; context_window_source?: "configured" | "provider"; reasoning_efforts?: string[] };
export type ModelCatalog = { route: string; metadata: Record<string, ModelMetadata> };

export function ModelSelector({ token, scope, provider = "", revision = "", idPrefix = "", model, error, disabled, preview = false, update, autoLoad = false, onCatalog }: {
  token: string; scope: SettingsScope; provider?: string; model: string; error?: string;
  disabled: boolean; preview?: boolean; update: (model: string) => void;
  autoLoad?: boolean; onCatalog?: (catalog: ModelCatalog) => void;
  revision?: string; idPrefix?: string;
}) {
  const modelId = idPrefix ? `${idPrefix}-settings-model` : "settings-model";
  const [models, setModels] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [metadata, setMetadata] = useState<Record<string, ModelMetadata>>({});
  const reading = useRef<AbortController>(undefined);
  const attempted = useRef(false);
  const route = `${scope}/${provider}/${revision}`;
  useEffect(() => {
    reading.current?.abort();
    setModels([]); setMetadata({}); setNotice(""); setQuery(""); setLoaded(false); setLoading(false); attempted.current = false;
    return () => reading.current?.abort();
  }, [route, token]);
  useEffect(() => {
    if (autoLoad && !disabled && !attempted.current) { attempted.current = true; void discover(); }
  }, [autoLoad, disabled, route, token]);

  async function discover() {
    reading.current?.abort();
    const controller = new AbortController(); reading.current = controller;
    setLoading(true); setNotice("");
    try {
      let id = provider;
      if (!id && scope === "global") {
        const registry = await (await request("provider-scopes/global/providers", token, undefined, controller.signal)).json() as { active: string };
        id = registry.active;
        if (!id) throw new Error("Select a global connection to browse models, or enter a model ID.");
      }
      const endpoint = id ? `provider-scopes/${scope}/providers/${encodeURIComponent(id)}/models` : "models";
      const result = await (await request(endpoint, token, undefined, controller.signal)).json() as { models: string[]; metadata?: Record<string, ModelMetadata> };
      if (controller.signal.aborted || reading.current !== controller) return;
      if (!Array.isArray(result.models) || !result.models.every(item => typeof item === "string")) throw new Error("Catalog unavailable; enter an ID manually.");
      setModels(result.models); setLoaded(true);
      setMetadata(result.metadata || {}); onCatalog?.({ route, metadata: result.metadata || {} });
      setNotice(`${result.models.length} models from this connection.`);
    } catch (cause) {
      if (!controller.signal.aborted) setNotice(cause instanceof Error ? cause.message : "Catalog unavailable; enter an ID manually.");
    } finally { if (reading.current === controller) { setLoading(false); reading.current = undefined; } }
  }
  const filtered = models.filter(id => id.toLowerCase().includes(query.trim().toLowerCase()));
  return <div className="settings-field settings-model-browser">
    <div className="model-browser-search"><Icon name="search" size={16} /><input type="search" aria-label="Search models" placeholder="Search available models…" value={query} disabled={disabled || !loaded} onChange={event => setQuery(event.target.value)} /><button type="button" disabled={disabled || loading} onClick={() => void discover()}>{loading ? "Loading…" : loaded ? "Refresh" : "Browse model catalog"}</button></div>
    {notice && <p className="model-catalog-status" role="status">{notice}</p>}
    {loaded && <ul className="model-catalog-list" aria-label="Catalog model IDs">{filtered.slice(0, 100).map(id => <li key={id}><button type="button" disabled={disabled || preview} aria-pressed={!preview && model === id} onClick={() => update(id)}><span><strong>{id}</strong><small>{metadata[id]?.context_window ? `${metadata[id].context_window!.toLocaleString()} context · ${metadata[id].context_window_source === "configured" ? "configured" : "provider"}` : id === model && !preview ? "Selected model" : "Available on this connection"}</small></span><span className="model-selection-mark" aria-hidden="true">{id === model && !preview ? "✓" : ""}</span></button></li>)}{!filtered.length && <li className="model-catalog-empty">No models match this search.</li>}</ul>}
    {filtered.length > 100 && <p>Showing the first 100 matches. Refine your search to find a model.</p>}
    <div className="settings-custom-model"><label htmlFor={modelId}>Model ID</label><input id={modelId} value={model} disabled={disabled || preview} autoComplete="off" spellCheck={false}
      maxLength={200} aria-invalid={!!error} aria-describedby={`${modelId}-help`} onChange={event => update(event.target.value)} />
      <p id={`${modelId}-help`}>{preview ? "Use this connection to choose or enter a model." : "Choose from the catalog, or enter a model ID. Agent profile overrides take precedence."}</p>
      {error && <p className="error-text">{error}</p>}
    </div>
  </div>;
}
