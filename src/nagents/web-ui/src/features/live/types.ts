export interface LiveCapability {
  available: boolean;
  reason: string;
  provider?: string;
  revision: string;
  enabled: boolean;
  key_configured: boolean;
  model: string;
  backend_model: string;
  backend_mode: "assistant" | "hosted";
  assistant: { provider: string; model: string; agent: string };
  voice: string;
  voices: string[];
  active_session_id: string;
}

export interface LiveCreated {
  session_id: string;
  model: string;
  voice: string;
}

export interface LiveSettingsValues {
  enabled: boolean;
  backend_mode: "assistant" | "hosted";
  provider: string;
  model: string;
  backend_model: string;
  voice: string;
  base_url: string;
}

export type VoiceScope = "global" | "workspace";
export type VoicePreferences = Pick<LiveSettingsValues, "enabled" | "backend_mode" | "model" | "backend_model" | "voice">;
export type VoiceOverrides = Partial<VoicePreferences>;

export interface LiveSettingsSnapshot {
  values: LiveSettingsValues;
  revision: string;
  key_configured: boolean;
  providers: string[];
  voices: string[];
  source?: "providers";
  profile_name?: string;
  connection_scope?: "global" | "workspace";
  api_key_env?: string;
  auth?: string;
  live_supported?: boolean;
  scope: VoiceScope;
  global_preferences: VoicePreferences;
  overrides: VoiceOverrides;
  origins: Record<keyof VoicePreferences, VoiceScope>;
}

export type LiveSettingsInput = {
  revision: string;
  scope: "global";
  preferences: VoicePreferences;
} | {
  revision: string;
  scope: "workspace";
  overrides: VoiceOverrides;
};

export interface LiveEvent {
  seq: number;
  type: "transcript" | "error" | "status";
  speaker?: "user" | "assistant";
  text?: string;
  message?: string;
  start_ms?: number;
  end_ms?: number;
}

export interface LiveSnapshot {
  session_id: string;
  status: "connecting" | "connected" | "closing" | "closed" | "error";
  model: string;
  voice: string;
  events: LiveEvent[];
  cursor: number;
  message?: string;
}

export interface Caption {
  id: number;
  speaker: "user" | "assistant";
  text: string;
  start: number;
  end: number;
}

export type LivePhase = "idle" | "permission" | "connecting" | "connected" | "ending" | "ended" | "error";

export interface LiveState {
  phase: LivePhase;
  sessionId: string;
  error: string;
  notice: string;
  micMuted: boolean;
  outputMuted: boolean;
  playbackBlocked: boolean;
  startedAt: number;
  endedAt: number;
  captions: Caption[];
}

export interface MediaHandlers {
  connected(): void;
  failed(message: string): void;
  playbackBlocked(blocked: boolean): void;
}

export interface LiveMedia {
  prepare(signal: AbortSignal): Promise<void>;
  connect(sessionId: string, token: string): Promise<void>;
  muteInput(muted: boolean): void;
  muteOutput(muted: boolean): void;
  play(): Promise<void>;
  close(): void;
}
