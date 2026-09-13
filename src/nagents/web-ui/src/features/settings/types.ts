import type { DictationConfig } from "../dictation/types.js";

export type SettingsValues = {
  model: string;
  agent: string;
  provider: string;
  base_url: string;
  api: string;
  auth: string;
  api_key_env: string;
  shell_timeout: number;
  max_output: number;
  max_file_bytes: number;
  max_tool_rounds: number;
  max_subagent_depth: number;
  dictation_enabled: boolean;
  dictation_model: string;
  dictation_language: string;
  dictation_max_seconds: number;
};

export type SettingsProfile = {
  name: string;
  mode: "build" | "reviewer";
  model: string;
};

export type SettingsConnection = {
  provider: string;
  api: string;
  auth: string;
  base_url: string;
  api_key_env: string;
  key_configured: boolean;
  auth_status: string;
};

export type SettingsReply = {
  values: SettingsValues;
  defaults: SettingsValues;
  profiles: SettingsProfile[];
  revision: string;
  persisted: boolean;
  effective_mode: "build" | "reviewer";
  providers: string[];
  apis: string[];
  auths: string[];
  connection: SettingsConnection;
  dictation: DictationConfig;
};
