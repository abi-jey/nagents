import type { DictationConfig } from "./features/dictation/types";
import type { LocalDelivery } from "./features/chat/deliveries.js";
import type { UploadMetadata } from "./features/chat/uploads.js";

export type Bootstrap = {
  token: string;
  workspace: string;
  provider: string;
  model: string;
  agent: string;
  demo: boolean;
  dictation: DictationConfig;
  active_run_id: string;
  active_session_id?: string;
  active_run_background?: boolean;
};

export type Session = {
  id: string; title: string; updated_at: string;
  parent_session_id?: string;
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
  id: string;
  name: string;
  status: "running" | "completed" | "failed" | "cancelled";
  result: string;
  error: string;
  session_id: string;
  prompt: string;
  child_session_id: string;
  parent_task_id: string;
  parent_session_id: string;
  depth: number;
  profile: string;
  mode: string;
  followups: number;
  activation: number;
  trigger: "delegation" | "human" | "wakeup" | "notification";
};
// Native parts returned by the backend for a saved multimodal message.
export type MessagePart =
  | { type: "text"; text: string }
  | { type: "image"; media_type: string; data_base64: string }
  | { type: "document"; media_type: string; title: string; data_base64: string }
  | { type: "audio"; format: string; data_base64: string };

export type Snapshot = {
  session_id: string;
  activity_cursor?: number;
  retained_tasks: RetainedTask[];
  sessions: Session[];
  active_run?: ActiveRun | null;
  active_run_id?: string;
  active_session_id?: string;
  history: {
    role: string;
    deliveries?: LocalDelivery[];
    uploads?: UploadMetadata[];
    history_id?: string;
    ingress_id?: string | number;
    // Missing on legacy replies; only explicit true authorizes channel source metadata.
    source_verified?: boolean;
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

export type ContextStats = {
  components: ContextComponent[];
  total_tokens: number;
  context_window: number | null;
  remaining_tokens: number | null;
  observed_prompt_tokens: number | null;
  observed_completion_tokens: number | null;
  provider: string;
  model: string;
  estimate_method: string;
};

export type Approval = {
  run_id: string;
  approval_id: string;
  call_id: string;
  tool: string;
  description: string;
  preview: string;
  arguments: unknown;
  task_id: string;
  task_name: string;
  depth: number;
  activation: number;
};

export type Decision = "allow" | "deny";
export type WireEvent = { event: string; [key: string]: unknown };

export type PendingWakeup = {
  wakeup_id: string;
  task_id: string;
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
