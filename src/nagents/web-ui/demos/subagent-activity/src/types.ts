/** Transport-independent records. The demo consumes events; it does not call ngn. */
export type AgentId = string;
export type Status = "running" | "waiting" | "failed" | "completed";
export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export interface AgentDefinition {
  id: AgentId;
  parentId: AgentId;
  name: string;
  role: string;
  model: string;
  provider: string;
  initials: string;
  color: "blue" | "violet" | "teal" | "amber";
  assignment: string;
  spawnedAt: number;
  originMessageId: string;
}

export interface MainMessage {
  id: string;
  at: number;
  role: "assistant" | "user";
  text: string;
  returnedAgentId?: AgentId;
  originMessageId?: string;
}

export interface Message extends MainMessage {
  delivery?: "queued" | "picked_up";
  pickedUpAt?: number;
}

export interface ToolCall {
  id: string;
  name: string;
  title: string;
  arguments: { [key: string]: JsonValue };
  startedAt: number;
  finishedAt?: number;
  status: "running" | "waiting" | "completed" | "failed";
  summary: string;
  output: string;
  format: "text" | "json" | "shell";
}

export type RecordItem = { kind: "message"; message: Message } | { kind: "tool"; tool: ToolCall };

export interface AgentRun {
  definition: AgentDefinition;
  status: Status;
  activity: string;
  startedAt: number;
  finishedAt?: number;
  records: RecordItem[];
}

/** Feed events in order from any future SSE/WebSocket/queue adapter. */
export type AgentEvent = { id: string; at: number; agentId: AgentId } & (
  | { type: "status"; status: Status; activity: string }
  | { type: "message"; role: "assistant" | "user"; text: string }
  | { type: "tool_start"; tool: Omit<ToolCall, "startedAt" | "finishedAt"> }
  | { type: "tool_result"; toolId: string; status: "completed" | "failed"; summary: string; output: string }
  | { type: "response_boundary" }
  | { type: "return"; text: string }
);

export interface ActivityState {
  now: number;
  agents: Record<AgentId, AgentRun>;
  mainMessages: MainMessage[];
  /** Scheduled fixture acknowledgements, independent of each child's queue. */
  pendingMainReplies: { id: string; at: number; originMessageId: string }[];
  seenEvents: string[];
  nextMessageId: number;
  /** A finished demo agent can receive a new turn; this is not a tool interrupt. */
  followUps: Record<AgentId, number>;
  announcement: string;
}
