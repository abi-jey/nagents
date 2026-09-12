export type DictationConfig = {
  enabled: boolean;
  available: boolean;
  admin_enabled: boolean;
  status: string;
  api_key_env: string;
  max_seconds: number;
  max_bytes: number;
  sample_rate: 16000;
  channels: 1;
  sample_width: 2;
  content_type: "audio/wav";
  revision: string;
};

export type DictationContext = {
  token: string;
  sessionId: string;
  config: DictationConfig;
};

export type DictationState =
  | { phase: "idle" | "error"; message: string }
  | { phase: "permission" | "recording" | "stopping" | "transcribing" }
  | { phase: "recorded"; seconds: number }
  | { phase: "review"; text: string; error: string };

export type RecordedAudio = { blob: Blob; seconds: number; limited: boolean };
export type RecordingCallbacks = {
  stopped: (audio: RecordedAudio) => void;
  failed: (cause: unknown) => void;
};
export type Recording = {
  start: () => Promise<void>;
  stop: () => void;
  cancel: () => void;
};
