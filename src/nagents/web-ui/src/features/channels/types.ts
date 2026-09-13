export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export type Property = {
  type?: string;
  title?: string;
  description?: string;
  writeOnly?: boolean;
  readOnly?: boolean;
  pattern?: string;
  additionalProperties?: boolean;
  enum?: Json[];
  default?: Json;
  items?: Property;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  properties?: Record<string, Property>;
  required?: string[];
  anyOf?: Property[];
  oneOf?: Property[];
  allOf?: Property[];
};
export type Schema = Property;
export type ChannelPlugin = { id: string; name: string; description: string; version: string; schema: Schema };
export type Connection = {
  id: string; plugin: string; enabled: boolean; status: string; error: string;
  config: JsonObject; secret_fields: string[]; configured_secrets: string[]; main_session_id: string;
};
export type Catalog = {
  revision: string; plugin_path: string; plugins: ChannelPlugin[]; connections: Connection[];
  bindings: { channel: string; conversation_id: string; session_id: string }[];
};
export type ChannelDraft = {
  id: string; plugin: string; enabled: boolean; mainSessionId: string;
  fields: Record<string, string>; json: string; secrets: Record<string, string>; clear: string[];
};
export type ChannelSave = {
  revision: string; plugin: string; enabled: boolean; main_session_id: string;
  config: JsonObject; secrets: JsonObject;
};
