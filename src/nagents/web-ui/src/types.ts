import type { LocalDelivery } from "./features/chat/deliveries.js";
import type { UploadMetadata } from "./features/chat/uploads.js";
import type { SessionAgentSelection } from "./features/sessions/sessionAgent.js";

export type Bootstrap = {
  token: string;
  workspace: string;
  provider: string;
  model: string;
  agent: string;
  demo: boolean;
  provider_setup?: { configured: boolean; message: string };
  active_run_id: string;
  active_session_id?: string;
  active_run_background?: boolean;
  active_runs?: ActiveRun[];
};

export type Session = {
  id: string; title: string; updated_at: string;
  parent_session_id?: string;
  forked_from?: string;
  active_run_id?: string;
  status?: string;
};
export type ActiveRun = {
  id: string;
  status: string;
  session_id?: string;
  events?: WireEvent[];
  records?: WireEvent[];
  message_id?: string;
  history_id?: string;
  ingress_id?: string | number;
  pending_approvals?: WireEvent[];
  approval?: WireEvent | Record<string, never>;
};
export type RetainedTask = {
  session_id: string;
  title: string;
  status: "running" | "completed" | "failed" | "cancelled";
  result: string;
  error: string;
  root_session_id: string;
  prompt: string;
  parent_session_id: string;
  depth: number;
  profile: string;
  mode: string;
  followups: number;
  activation: number;
  trigger: "delegation" | "human" | "wakeup" | "notification";
  provider?: string;
  provider_kind?: string;
  model?: string;
  phase?: string;
  started_at?: number;
  updated_at?: number;
  completed_at?: number;
  activity_sequence?: number;
  can_message?: boolean;
  can_start_children?: boolean;
  child_agents?: string[];
  child_start_unavailable_reason?: string;
  message_unavailable_reason?: string;
  unavailable_reason?: string;
  phase_detail?: string;
  compaction?: CompactionStats;
  reasoning_effort?: string;
  next_settings?: { provider?: string; model?: string; reasoning_effort?: string; compact_trigger?: string; compact_tokens?: number; compact_messages?: number };
  origin_history_id?: string;
  origin_run_id?: string;
  activation_history_id?: string;
  activation_run_id?: string;
  activation_call_id?: string;
  activation_call_position?: number;
  compact_trigger?: string;
  compact_tokens?: number;
  compact_messages?: number;
  context?: ContextStats;
};
// Native parts returned by the backend for a saved multimodal message.
export type MessagePart =
  | { type: "text"; text: string }
  | { type: "image"; media_type: string; data_base64: string }
  | { type: "document"; media_type: string; title: string; data_base64: string }
  | { type: "audio"; format: string; data_base64: string };

export type Snapshot = {
  session_id: string;
  agent_selection?: SessionAgentSelection;
  activity_cursor?: number;
  retained_tasks: RetainedTask[];
  retained_activations?: RetainedTask[];
  sessions: Session[];
  active_run?: ActiveRun | null;
  active_run_id?: string;
  active_session_id?: string;
  active_runs?: ActiveRun[];
  history: {
    role: string;
    deliveries?: LocalDelivery[];
    uploads?: UploadMetadata[];
    history_id?: string;
    response_id?: string;
    response_metadata?: unknown;
    agent?: string;
    previous_agent?: string;
    model?: string;
    provider?: string;
    ingress_id?: string | number;
    // Missing on legacy replies; only explicit true authorizes channel source metadata.
    source_verified?: boolean;
    // Only the host's admitted voice input may mark a saved row as voice.
    voice_verified?: boolean;
    voice_session_id?: string;
    caption_seq?: number;
    speaker?: "user" | "assistant";
    start_ms?: number;
    end_ms?: number;
    anchor_history_id?: string;
    message_id?: string;
    source?: unknown;
    parts?: MessagePart[];
    content: string;
    name: string;
    tool_call_id: string;
    tool_calls: { id: string; name: string; arguments: unknown }[];
  }[];
};

export type ContextComponent = { key: string; label: string; tokens: number };

export type CompactionStats = {
  trigger: string;
  token_threshold?: number;
  token_budget?: number;
  message_threshold?: number;
};

export type ContextStats = {
  components: ContextComponent[];
  total_tokens: number;
  context_window: number | null;
  context_window_source: "configured" | "provider" | "heuristic";
  remaining_tokens: number | null;
  observed_prompt_tokens: number | null;
  observed_completion_tokens: number | null;
  observed_reasoning_tokens: number | null;
  observed_reasoning_in_output: "included" | "separate" | "unknown";
  observed_context_tokens: number | null;
  pending_context_tokens: number;
  context_tokens: number;
  context_tokens_source: "provider" | "mixed" | "estimated";
  provider: string;
  model: string;
  estimate_method: string;
  compaction?: CompactionStats;
};

export type Approval = {
  run_id: string;
  approval_id: string;
  call_id: string;
  tool: string;
  description: string;
  preview: string;
  arguments: unknown;
  session_id: string;
  title: string;
  depth: number;
  activation: number;
  allow_tool?: boolean;
  allow_tool_persistent?: boolean;
};

export type Decision = "allow" | "allow_tool" | "deny";
export type WireEvent = { event: string; [key: string]: unknown };

export type PendingWakeup = {
  wakeup_id: string;
  session_id: string;
  due_at: string;
  reason: string;
};

export type ActivityReply = {
  session_id: string;
  cursor: number;
  events: WireEvent[];
  active_run_id: string;
  pending_wakeups: PendingWakeup[];
  truncated: boolean;
};
