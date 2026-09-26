export interface LiveCapability {
  available: boolean;
  reason: string;
  provider?: string;
  model: string;
  backend_model: string;
  voice: string;
  voices: string[];
  active_session_id: string;
}

export interface LiveCreated {
  session_id: string;
  sdp: string;
  model: string;
  voice: string;
}

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
  offer(signal: AbortSignal): Promise<string>;
  answer(sdp: string): Promise<void>;
  muteInput(muted: boolean): void;
  muteOutput(muted: boolean): void;
  play(): Promise<void>;
  close(): void;
}
