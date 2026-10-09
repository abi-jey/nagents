import type { PlaybackHealth } from "./playback.js";
import type { AudioFrame } from "../../components/voiceSphere/types.js";

export type LiveTransport = "websocket";
export type VoiceContextMode = "recent" | "summary" | "none";

export interface AudioDeviceSelection {
  inputId: string;
  outputId: string;
}

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
  transport?: LiveTransport;
  voice_auth?: "chatgpt" | "api-key" | "entra";
  context_mode?: VoiceContextMode;
}

export interface VoiceContext {
  mode: VoiceContextMode;
  method: "recent" | "existing_summary" | "generated_summary" | "recent_fallback" | "none";
  chat_session_id: string;
  fingerprint: string;
  message_count: number;
  characters: number;
  bytes: number;
  summary_included: boolean;
  omitted_messages: number;
  omitted_content: boolean;
  notice: string;
}

export interface LiveCreated {
  session_id: string;
  model: string;
  voice: string;
  context?: VoiceContext;
}

export interface LiveSettingsValues {
  enabled: boolean;
  connection_id?: string;
  backend_mode: "assistant" | "hosted";
  provider: string;
  model: string;
  backend_model: string;
  voice: string;
  base_url: string;
  instructions?: string;
  context_mode?: VoiceContextMode;
}

export type VoiceScope = "global" | "workspace";
export type VoicePreferences = Pick<LiveSettingsValues, "enabled" | "connection_id" | "backend_mode" | "model" | "backend_model" | "voice" | "instructions" | "context_mode">;
export type VoiceOverrides = Partial<VoicePreferences>;

export interface LiveSettingsSnapshot {
  values: LiveSettingsValues;
  revision: string;
  key_configured: boolean;
  providers: string[];
  voices: string[];
  connections?: { name: string; provider: string; scope: VoiceScope }[];
  source?: "providers";
  profile_name?: string;
  connection_scope?: "global" | "workspace";
  api_key_env?: string;
  auth?: string;
  voice_auth?: "chatgpt" | "api-key" | "entra";
  live_supported?: boolean;
  scope: VoiceScope;
  global_preferences: VoicePreferences;
  overrides: VoiceOverrides;
  origins: { [Key in keyof VoicePreferences]: VoiceScope };
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

export type LiveDelegationStatus = "queued" | "working" | "completed" | "failed" | "cancelled";

export interface LiveDelegationRecord {
  seq: number;
  delegation_id: string;
  status: LiveDelegationStatus;
  agent: string;
  provider: string;
  model: string;
  run_id: string;
  text: string;
  voice_session_id: string;
  chat_session_id: string;
}

export interface LiveDelegation {
  id: string;
  sessionId: string;
  chatSessionId: string;
  seq: number;
  status: LiveDelegationStatus;
  agent: string;
  provider: string;
  model: string;
  runId: string;
  text: string;
}

export interface DelegationText {
  text: string;
  truncated: boolean;
  characters: number;
  characters_complete?: boolean;
}

export type InspectionEventType = "delegation" | "model_context" | "http_request_body";

export interface LiveModelRequest {
  seq: number;
  type: "model_context" | "http_request_body";
  model_call_id: string;
  round: number;
  attempt_id?: string;
  segmented?: boolean;
  payload: DelegationText;
}

export interface LiveDelegationTimelineEvent extends LiveDelegationRecord {
  type?: InspectionEventType;
  model_call_id?: string;
  round?: number;
  attempt_id?: string;
  segmented?: boolean;
  capture_limited?: true;
}

export interface VoiceContextDetailsRecord extends VoiceContext {
  voice_session_id: string;
  available: boolean;
  instructions: DelegationText;
  history: { type: "message"; role: "user" | "assistant"; content: { type: "input_text" | "output_text"; text: string }[] }[];
  history_truncated: boolean;
  reason: string;
}

export interface LiveDelegationDetails extends LiveDelegationRecord {
  source: "app_callback";
  request: { transcript?: DelegationText; input?: DelegationText };
  result?: DelegationText & { kind: "assistant_output" | "terminal_explanation" };
  timeline: LiveDelegationTimelineEvent[];
  timeline_truncated: boolean;
  model_requests?: LiveModelRequest[];
  model_requests_truncated?: boolean;
}

export interface LiveMessageEvent {
  seq: number;
  type: "transcript" | "error" | "status";
  speaker?: "user" | "assistant";
  text?: string;
  message?: string;
  start_ms?: number;
  end_ms?: number;
}

export type LiveEvent = LiveMessageEvent | (LiveDelegationRecord & { type: "delegation" });

export interface LiveSnapshot {
  session_id: string;
  status: "connecting" | "connected" | "closing" | "closed" | "error";
  model: string;
  voice: string;
  events: LiveEvent[];
  cursor: number;
  message?: string;
  delegations?: LiveDelegationRecord[];
  context?: VoiceContext;
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
  inputLevel: number;
  outputLevel: number;
  startedAt: number;
  endedAt: number;
  captions: Caption[];
  delegations: LiveDelegation[];
  devices: AudioDeviceSelection;
  context?: VoiceContext;
}

export interface MediaHandlers {
  connected(): void;
  ended(): void;
  failed(message: string): void;
  playbackBlocked(blocked: boolean): void;
  levels?(input: number, output: number): void;
}

export interface LiveMedia {
  /** Optional, read-only visualization of the media owned by this connection. */
  audioHealth?(): PlaybackHealth;
  sampleAudio?(): AudioFrame;
  prepare(signal: AbortSignal): Promise<void>;
  connect(sessionId: string, token: string): Promise<void>;
  muteInput(muted: boolean): void;
  muteOutput(muted: boolean): void;
  setInputDevice(deviceId: string): Promise<void>;
  setOutputDevice(deviceId: string): Promise<void>;
  play(): Promise<void>;
  close(): void;
}
