import { RequestError } from "../../api/client.js";
import { object } from "../../api/subscription.js";
import type { Catalog, ChannelSave } from "./types.js";

function schema(value: unknown, depth = 0): boolean {
  if (!object(value) || depth > 20) return false;
  return ["title", "description", "pattern"].every((key) => value[key] === undefined || typeof value[key] === "string") &&
    ["writeOnly", "readOnly"].every((key) => value[key] === undefined || typeof value[key] === "boolean") &&
    (value.enum === undefined || Array.isArray(value.enum)) &&
    (value.required === undefined || (Array.isArray(value.required) && value.required.every((key: unknown) => typeof key === "string"))) &&
    (value.properties === undefined || (object(value.properties) && Object.values(value.properties).every((field) => schema(field, depth + 1)))) &&
    (value.items === undefined || schema(value.items, depth + 1)) &&
    ["anyOf", "oneOf", "allOf"].every((key) => value[key] === undefined || (Array.isArray(value[key]) && value[key].every((field: unknown) => schema(field, depth + 1))));
}

export async function channelRequest(token: string, signal: AbortSignal, operation: "load" | "refresh" | "save" | "delete", id = "", body?: ChannelSave | { revision: string }): Promise<Catalog> {
  const path = operation === "refresh" ? "channels/refresh" : operation === "load" ? "channels" : `channels/${encodeURIComponent(id)}`;
  const payload = operation === "refresh" ? {} : body;
  const response = await fetch(`/api/${path}`, {
    method: { load: "GET", refresh: "POST", save: "PUT", delete: "DELETE" }[operation],
    headers: { "X-Ngn-Token": token, ...(payload ? { "Content-Type": "application/json" } : {}) },
    body: payload ? JSON.stringify(payload) : undefined, signal, cache: "no-store", credentials: "same-origin",
  });
  if (!response.ok) throw new RequestError(channelError(response.status), response.status);
  const value: unknown = await response.json();
  signal.throwIfAborted();
  if (!object(value) || typeof value.revision !== "string" || typeof value.plugin_path !== "string" ||
      !Array.isArray(value.plugins) || !value.plugins.every((plugin: unknown) => object(plugin) &&
        ["id", "name", "description", "version"].every((key) => typeof plugin[key] === "string") && schema(plugin.schema)) ||
      !Array.isArray(value.connections) || !value.connections.every((connection: unknown) => object(connection) &&
        ["id", "plugin", "status", "error", "main_session_id"].every((key) => typeof connection[key] === "string") &&
        typeof connection.enabled === "boolean" && object(connection.config) &&
        ["secret_fields", "configured_secrets"].every((key) => Array.isArray(connection[key]) && connection[key].every((item: unknown) => typeof item === "string"))) ||
      !Array.isArray(value.bindings) || !value.bindings.every((binding: unknown) => object(binding) &&
        ["channel", "conversation_id", "session_id"].every((key) => typeof binding[key] === "string"))) throw new Error("Invalid channel catalog. Refresh to try again.");
  return value as Catalog;
}
export function channelError(status: number): string {
  if (status === 409) return "Channel configuration changed or the harness is busy. Your draft is kept. Refresh, review the latest configuration, then Save again.";
  if (status === 401 || status === 403) return "Channel access expired. Close this dialog and reconnect to the local harness.";
  if (status === 404) return "Channel or plugin unavailable. Refresh installed plugins and check the instance ID.";
  if (status === 413) return "Channel configuration is too large. Reduce the configuration and try again.";
  if (status === 400 || status === 422) return "Configuration was rejected. Check required fields, secret choices and the main session, then Save again.";
  return "Channel operation was not confirmed. Refresh to check the saved configuration before retrying.";
}
