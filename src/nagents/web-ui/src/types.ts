export type Bootstrap = {
  token: string;
  workspace: string;
  provider: string;
  model: string;
  agent: string;
  demo: boolean;
  active_run_id: string;
};

export type Session = { id: string; title: string; updated_at: string };
export type Snapshot = {
  session_id: string;
  sessions: Session[];
  history: {
    role: string;
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
};

export type Decision = "allow" | "deny";
export type WireEvent = { event: string; [key: string]: unknown };
