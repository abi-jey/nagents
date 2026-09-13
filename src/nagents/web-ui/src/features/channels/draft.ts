import type { Session } from "../../types.js";
import type { Catalog, ChannelDraft, ChannelPlugin, ChannelSave, Connection, Json, JsonObject, Property } from "./types.js";

export function rootSessions(sessions: Session[]): Session[] {
  return sessions.filter((session) => !session.parent_session_id);
}
export function secretFields(plugin?: ChannelPlugin, connection?: Connection): string[] {
  function privateField(field: Property): boolean {
    return !!field.writeOnly || Object.values(field.properties || {}).some(privateField) ||
      (!!field.items && privateField(field.items)) || [...(field.anyOf || []), ...(field.oneOf || []), ...(field.allOf || [])].some(privateField);
  }
  return [...new Set([
    ...Object.entries(plugin?.schema.properties || {}).filter(([, field]) => privateField(field)).map(([key]) => key),
    ...(connection?.secret_fields || []), ...(connection?.configured_secrets || []),
  ])];
}
export function validSecretName(name: string): boolean {
  return /^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/.test(name) && !["name", "__proto__", "constructor", "prototype"].includes(name);
}
export function hostProperty(key: string, field?: Property): boolean {
  return key === "name" || field?.readOnly === true;
}
export function simple(field: Property): boolean {
  return !!field.enum || ["string", "number", "integer", "boolean", "array"].includes(field.type || "");
}
function display(value: Json | undefined, field: Property): string {
  return value === undefined ? "" : field.type === "string" && !field.enum && typeof value === "string"
    ? value : JSON.stringify(value);
}
export function makeDraft(catalog: Catalog, selected: string, connection?: Connection, pluginId = ""): ChannelDraft {
  const plugin = catalog.plugins.find((item) => item.id === (connection?.plugin || pluginId || catalog.plugins[0]?.id));
  const secrets = secretFields(plugin, connection);
  const config = Object.fromEntries(Object.entries(connection?.config || {}).filter(([key]) =>
    !secrets.includes(key) && !hostProperty(key, plugin?.schema.properties?.[key])));
  const fields: Record<string, string> = {};
  for (const [key, field] of Object.entries(plugin?.schema.properties || {})) {
    if (hostProperty(key, field) || secrets.includes(key) || !simple(field)) continue;
    fields[key] = display(config[key] ?? field.default, field);
    delete config[key];
  }
  return {
    id: connection?.id || "", plugin: connection?.plugin || plugin?.id || "", enabled: connection?.enabled ?? true,
    mainSessionId: connection?.main_session_id || selected, fields,
    json: JSON.stringify(config, null, 2), secrets: {}, clear: [],
  };
}
function validate(value: Json, field: Property): string {
  if (field.enum && !field.enum.some((item) => JSON.stringify(item) === JSON.stringify(value))) return "Choose an available value.";
  if (field.type === "string") {
    if (typeof value !== "string") return "Enter text.";
    if (field.minLength !== undefined && value.length < field.minLength) return `Use at least ${field.minLength} characters.`;
    if (field.maxLength !== undefined && value.length > field.maxLength) return `Use at most ${field.maxLength} characters.`;
    if (field.pattern) {
      try { if (!new RegExp(field.pattern, "u").test(value)) return "Use the format required by this field."; }
      catch { return "This field's schema pattern is invalid. Refresh the installed plugin."; }
    }
  }
  if (field.type === "boolean" && typeof value !== "boolean") return "Choose true or false.";
  if (field.type === "number" || field.type === "integer") {
    if (typeof value !== "number" || !Number.isFinite(value) || (field.type === "integer" && !Number.isInteger(value)))
      return field.type === "integer" ? "Enter a whole number." : "Enter a finite number.";
    if (field.minimum !== undefined && value < field.minimum) return `Minimum: ${field.minimum}.`;
    if (field.maximum !== undefined && value > field.maximum) return `Maximum: ${field.maximum}.`;
  }
  if (field.type === "array") {
    if (!Array.isArray(value)) return "Enter a JSON array.";
    if (field.minItems !== undefined && value.length < field.minItems) return `Include at least ${field.minItems} items.`;
    if (field.items && value.some((item) => validate(item, field.items!))) return "Check the array item types and allowed values.";
  }
  if (field.type === "object" && (!value || typeof value !== "object" || Array.isArray(value))) return "Enter a JSON object.";
  return "";
}
export function buildSave(catalog: Catalog, draft: ChannelDraft, sessions: Session[]): { errors: Record<string, string>; payload?: ChannelSave } {
  const errors: Record<string, string> = {};
  const plugin = catalog.plugins.find((item) => item.id === draft.plugin);
  const connection = catalog.connections.find((item) => item.id === draft.id);
  if (!/^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/.test(draft.id)) errors.id = "Use 1–64 letters, numbers, dots, underscores or hyphens; start with a letter or underscore.";
  if (!plugin) errors.plugin = "Refresh installed plugins and choose an available plugin.";
  if (!draft.mainSessionId || !rootSessions(sessions).some((item) => item.id === draft.mainSessionId))
    errors.mainSessionId = "Choose an existing root session for /session main.";
  let config: JsonObject = {};
  try {
    const value: unknown = JSON.parse(draft.json);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
    config = value as JsonObject;
  } catch { errors.json = "Enter a valid JSON object for additional configuration."; }
  const secretNames = [...new Set([...secretFields(plugin, connection), ...Object.keys(draft.secrets), ...draft.clear])];
  if ("name" in config) errors.json = "The instance name is set by the stable instance ID; remove name from public JSON.";
  if (secretNames.some((key) => key in config)) errors.json = "Move secret fields to the password inputs; secrets cannot be saved in public JSON.";
  if (Object.keys(config).some((key) => plugin?.schema.properties?.[key]?.readOnly))
    errors.json = "Read-only properties are supplied by the host; remove them from public JSON.";
  if (plugin?.schema.additionalProperties === false && Object.keys(config).some((key) => !Object.hasOwn(plugin.schema.properties || {}, key)))
    errors.json = "Use only properties declared by this plugin.";
  for (const [key, field] of Object.entries(plugin?.schema.properties || {})) {
    if (hostProperty(key, field) || secretNames.includes(key)) continue;
    const raw = draft.fields[key];
    if (simple(field)) {
      if (key in config) errors.json = "Use the generated fields instead of duplicating them in additional JSON.";
      // Public config is a complete replacement, not a patch. Empty generated
      // fields omit the key, removing an old reference such as token_env.
      if (raw !== undefined && raw !== "") {
        try { config[key] = field.type === "string" && !field.enum ? raw : JSON.parse(raw) as Json; }
        catch { errors[key] = field.type === "array" ? "Enter a valid JSON array." : "Enter a valid value."; }
      }
    }
    if (config[key] !== undefined) errors[key] ||= validate(config[key], field);
  }
  const secrets: JsonObject = {};
  for (const key of secretNames) {
    if (!validSecretName(key)) { errors.json = "Check secret field names; instance name is server-controlled."; continue; }
    if (plugin?.schema.properties?.[key]?.readOnly) { errors.json = "Read-only properties are supplied by the host."; continue; }
    if (plugin?.schema.additionalProperties === false && !Object.hasOwn(plugin.schema.properties || {}, key)) {
      errors.json = "Use only secret properties declared by this plugin."; continue;
    }
    if (draft.clear.includes(key)) { secrets[key] = ""; continue; }
    const raw = draft.secrets[key];
    if (!raw) continue;
    const field = plugin?.schema.properties?.[key];
    try {
      const value = field?.type && field.type !== "string" ? JSON.parse(raw) as Json : raw;
      if (field) errors[key] = validate(value, field);
      secrets[key] = value;
    } catch { errors[key] = "Enter valid JSON for this secret property."; }
  }
  for (const key of plugin?.schema.required || []) {
    if (hostProperty(key, plugin?.schema.properties?.[key])) continue;
    if (secretNames.includes(key)) {
      if (!secrets[key] && (!connection?.configured_secrets.includes(key) || draft.clear.includes(key))) errors[key] = "Enter the required secret.";
    } else if (config[key] === undefined || config[key] === "") errors[key] = "This field is required.";
  }
  if (Object.values(errors).some(Boolean)) return { errors };
  return { errors: {}, payload: { revision: catalog.revision, plugin: draft.plugin, enabled: draft.enabled,
    main_session_id: draft.mainSessionId, config, secrets } };
}

function quote(value: string): string { return `'${value.replaceAll("'", "'\"'\"'")}'`; }
export function installCommand(path: string, packageName: string): string {
  // A distribution name or exact version, never a URL, option, shell fragment,
  // or requirements file. Quote the server-controlled path as one shell word.
  if (!path || /[\r\n\0]/.test(path) || !/^[A-Za-z0-9][A-Za-z0-9._-]*(?:==[A-Za-z0-9][A-Za-z0-9.!+_-]*)?$/.test(packageName)) return "";
  return `python -m pip install --target ${quote(path)} --no-deps ${quote(packageName)}`;
}
