import { useState } from "react";
import { filterModels, modelCatalogStatus } from "./modelCatalog.js";
import { useModelCatalog } from "./useModelCatalog.js";

export function ModelField({
  token,
  model,
  error,
  update,
}: {
  token: string;
  model: string;
  error?: string;
  update: (model: string) => void;
}) {
  const catalog = useModelCatalog(token);
  const [query, setQuery] = useState("");
  const results = filterModels(catalog.catalog?.models || [], query);

  return (
    <div className="settings-field">
      <label htmlFor="settings-model">Model ID</label>
      <div className="settings-model-controls">
        <input
          id="settings-model"
          type="text"
          value={model}
          autoComplete="off"
          spellCheck={false}
          required
          aria-describedby={`settings-model-help${error ? " settings-model-error" : ""}`}
          aria-invalid={!!error}
          onChange={(event) => update(event.target.value)}
        />
        <button
          type="button"
          disabled={catalog.loading}
          aria-describedby="settings-model-catalog-status"
          onClick={() => void catalog.fetchModels()}
        >
          {catalog.loading
            ? "Fetching models..."
            : catalog.attempted
              ? "Refresh models"
              : "Fetch models"}
        </button>
      </div>
      <p id="settings-model-help">
        Exact model ID for the current connection, up to 200 characters. You can
        always enter an ID, even if it is not listed. A catalog is not a guarantee
        of compatibility with the current API, tools, or account access.
      </p>
      {error && (
        <p id="settings-model-error" className="error-text">
          {error}
        </p>
      )}
      <p
        id="settings-model-catalog-status"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className={catalog.error ? "error-text" : ""}
      >
        {modelCatalogStatus(catalog)}
      </p>
      {!!catalog.catalog?.models.length && (
        <div className="settings-model-catalog" aria-busy={catalog.loading}>
          <label htmlFor="settings-model-search">Search fetched models</label>
          <input
            id="settings-model-search"
            type="text"
            enterKeyHint="search"
            value={query}
            autoComplete="off"
            spellCheck={false}
            aria-describedby="settings-model-results settings-model-catalog-status"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              // Searching must not implicitly submit the Settings form.
              if (event.key === "Enter") event.preventDefault();
            }}
          />
          <p id="settings-model-results" role="status" aria-live="polite">
            {results.length
              ? `${results.length} matching ${results.length === 1 ? "ID" : "IDs"}.`
              : "No matching IDs. Try another search or enter a model ID above."}
          </p>
          {!!results.length && (
            <ul className="settings-model-results" aria-label="Fetched models">
              {results.map((id, index) => (
                <li key={`${index}:${id}`}>
                  <button
                    type="button"
                    aria-current={model === id ? "true" : undefined}
                    onClick={() => update(id)}
                  >
                    {id}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
