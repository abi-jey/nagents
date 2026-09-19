export interface ToolSelection { ref: string; description: string }
export type Port = "top" | "right" | "bottom" | "left";
export interface Invocation {
  agent: string; description: string;
  source_port?: Port; target_port?: Port; source_offset?: number; target_offset?: number;
}
export interface AgentDefinition {
  name: string; provider: string; instructions: { text: string; file: string };
  tools: ToolSelection[]; mcp: { server: string; tools: ToolSelection[] }[];
  invokes: Invocation[]; max_tool_rounds: number;
}
export interface Design {
  version: 1; id: string; entrypoint: string;
  defaults: { provider: string; max_subagent_depth: number };
  secrets: Record<string, { source: "env" | "saved"; name: string }>;
  providers: Record<string, { type: string; model: string; base_url: string; api: string; api_version: string; secret: string; auth?: "api-key" | "chatgpt" }>;
  mcp_servers: Record<string, { transport: "stdio"; command: string; args: string[]; env: Record<string, string>; secrets: Record<string, string> }>;
  agents: Record<string, AgentDefinition>; layout: Record<string, { x: number; y: number }>;
}
export interface TraceRecord { sequence: number; kind: string; timestamp: string; data: Record<string, unknown> }
export interface RunSummary { id: string; agent: string; status: string; created: string; revision: string }
export interface TraceReply extends RunSummary {
  design: string; events: TraceRecord[];
  approval: { approval_id?: string; id?: string; description?: string; preview?: string };
}

export function newAgent(): AgentDefinition {
  return { name: "", provider: "", instructions: { text: "You are a helpful assistant.", file: "" }, tools: [], mcp: [], invokes: [], max_tool_rounds: 30 };
}

export function removeAgent(design: Design, id: string): Design {
  if (Object.keys(design.agents).length <= 1) return design;
  const next = structuredClone(design);
  delete next.agents[id]; delete next.layout[id];
  for (const agent of Object.values(next.agents)) agent.invokes = agent.invokes.filter((edge) => edge.agent !== id);
  if (next.entrypoint === id) next.entrypoint = Object.keys(next.agents)[0];
  return next;
}

export function appendTrace(current: TraceRecord[], incoming: TraceRecord[]): TraceRecord[] {
  const last = current.at(-1)?.sequence || 0;
  return [...current, ...incoming.filter((record) => record.sequence > last)];
}

export function traceMatches(record: TraceRecord, category: string): boolean {
  if (category !== "steps") return !category || record.kind.includes(category);
  if (["http_stream", "context_before_model"].includes(record.kind)) return false;
  if (record.kind === "agent_event") {
    const event = record.data.event as { type?: string } | undefined;
    return ["text_done", "error", "compaction_started", "compaction_done"].includes(event?.type || "");
  }
  if (record.kind === "execution_event") return ["task_started", "task_completed", "task_notification", "error"].includes(String(record.data.event));
  return true;
}
