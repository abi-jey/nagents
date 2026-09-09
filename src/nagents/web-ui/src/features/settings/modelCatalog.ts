import { RequestError, request } from "../../api/client.js";

export type ModelCatalog = { models: string[]; source: string };

export type ModelCatalogState = {
  catalog?: ModelCatalog;
  loading: boolean;
  attempted: boolean;
  error: string;
};

export async function readModelCatalog(
  token: string,
  signal: AbortSignal,
): Promise<ModelCatalog> {
  const data: unknown = await (
    await request("models", token, undefined, signal)
  ).json();
  // A replaced request may finish parsing even after its fetch was aborted.
  signal.throwIfAborted();
  if (
    !data ||
    typeof data !== "object" ||
    !("models" in data) ||
    !Array.isArray(data.models) ||
    !data.models.every(
      (model: unknown) => typeof model === "string" && model.trim().length > 0,
    ) ||
    !("source" in data) ||
    typeof data.source !== "string" ||
    !data.source.trim()
  ) {
    throw new SyntaxError("Invalid model catalog response.");
  }
  return { models: data.models, source: data.source };
}

export function modelCatalogFailure(cause: unknown): string {
  if (cause instanceof RequestError) {
    return `${cause.status === 501 ? "Model discovery is not supported." : "Could not fetch models."} ${cause.message}`;
  }
  if (cause instanceof SyntaxError) {
    return "The server returned an invalid model catalog. Try Refresh models.";
  }
  return "Could not reach the model catalog. Check the connection and try Refresh models.";
}

export function modelCatalogStatus(state: ModelCatalogState): string {
  const { catalog, loading, error } = state;
  const summary = catalog
    ? `${catalog.models.length} model ${catalog.models.length === 1 ? "ID" : "IDs"} fetched. Source: ${catalog.source}.`
    : "";
  if (loading) {
    return catalog
      ? `Refreshing models. ${summary} Previous results may be stale.`
      : "Fetching models from the active provider...";
  }
  if (error) {
    return `${error}${catalog ? ` ${summary} Showing previous results; they may be stale.` : ""} You can still enter a model ID and save manually.`;
  }
  if (catalog) {
    return `${summary}${catalog.models.length ? " Choose a result to edit the draft, then Save to apply." : " The provider returned no model IDs. You can still enter a model ID manually."}`;
  }
  return "Models have not been fetched. Manual entry is always available.";
}

export function filterModels(models: string[], query: string): string[] {
  const search = query.trim().toLowerCase();
  return models.filter((model) => model.toLowerCase().includes(search));
}
