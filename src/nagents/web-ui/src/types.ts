import type { DictationConfig } from "./features/dictation/types";

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
    message_id?: string;
    source?: unknown;
    content: string;
    name: string;
    tool_call_id: string;
    tool_calls: { id: string; name: string; arguments: unknown }[];
  }[];
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
