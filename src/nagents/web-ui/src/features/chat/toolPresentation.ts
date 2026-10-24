import type { IconName } from "../../components/Icon.js";
import { presentToolResult, toolObject, type ResultPresentation } from "./toolResultPresentation.js";

type ToolFamily = "command" | "read" | "edit" | "write" | "list" | "search" | "skill" | "team" | "schedule" | "compact" | "channel" | "mcp" | "generic";
type ToolPresentation = { label: string; icon: IconName; family: ToolFamily; origin?: string; method?: string };
const tools: Record<string, ToolPresentation> = {
  shell: { label: "Run command", icon: "terminal", family: "command" },
  run_shell_command: { label: "Run command", icon: "terminal", family: "command" },
  read_file: { label: "Read file", icon: "file-read", family: "read" },
  edit: { label: "Edit file", icon: "edit", family: "edit" },
  edit_file: { label: "Edit file", icon: "edit", family: "edit" },
  write: { label: "Write file", icon: "file-write", family: "write" },
  write_file: { label: "Write file", icon: "file-write", family: "write" },
  list_files: { label: "List files", icon: "folder", family: "list" },
  list_directory: { label: "List directory", icon: "folder", family: "list" },
  find: { label: "Find files", icon: "search", family: "list" },
  search: { label: "Search files", icon: "search", family: "search" },
  search_files: { label: "Search files", icon: "search", family: "search" },
  skill: { label: "Load skill", icon: "book", family: "skill" },
  to_agent: { label: "Send to agent", icon: "team", family: "team" },
  agent_status: { label: "Inspect agent status", icon: "team", family: "team" },
  team: { label: "Team", icon: "team", family: "team" },
  schedule_wakeup: { label: "Schedule wake-up", icon: "clock", family: "schedule" },
  wake_up_in: { label: "Schedule wake-up", icon: "clock", family: "schedule" },
  compact_history: { label: "Compact history", icon: "compact", family: "compact" },
  channel_list: { label: "List channels", icon: "channels", family: "channel" },
  channel_send: { label: "Send to channel", icon: "channels", family: "channel" },
  channel_action: { label: "Channel action", icon: "channels", family: "channel" },
  channel_configuration: { label: "Inspect channel configuration", icon: "channels", family: "channel" },
  channel_configure: { label: "Queue channel configuration", icon: "channels", family: "channel" },
};

export function toolPresentation(name: string, remoteReview?: { tool: string }): ToolPresentation {
  if (remoteReview) return { label: `Remote ${remoteReview.tool} review`, icon: tools[remoteReview.tool]?.icon || "tools", family: "generic" };
  if (Object.hasOwn(tools, name)) return tools[name];
  // MCPManager qualifies real names as mcp__<connection>__<method>.
  // Keep the original name in Details, including any further separators.
  const mcp = /^mcp__([^]+?)__(.+)$/.exec(name);
  if (mcp) return { label: `${mcp[1]} · ${mcp[2]}`, icon: "plug", family: "mcp", origin: mcp[1], method: mcp[2] };
  return { label: name, icon: "tools", family: "generic" };
}

type Validator = (value: unknown) => boolean;
type Shape = Record<string, Validator>;
const string: Validator = (value) => typeof value === "string";
const boolean: Validator = (value) => typeof value === "boolean";
const count: Validator = (value) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const positiveCount: Validator = (value) => count(value) && (value as number) > 0;
const delay: Validator = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 604800;
const strings: Validator = (value) => Array.isArray(value) && value.length <= 1000 && value.every(string);

function matches(value: Record<string, unknown>, required: Shape, optional: Shape = {}): boolean {
  const shape = { ...required, ...optional };
  return Object.keys(required).every((key) => Object.hasOwn(value, key))
    && Object.entries(value).every(([key, item]) => Object.hasOwn(shape, key) && shape[key](item));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

const field = (label: string, value: unknown) => ({ label, text: String(value) });
const section = (label: string, value: unknown) => ({ label, text: String(value) });
const truncation = (value: unknown) => value === true ? [{ text: "Output truncated by tool", warning: true }] : [];
type NativeView = Pick<ResultPresentation, "format" | "sections" | "metadata" | "diagnostics" | "indicators" | "note">;

function resultView(value: Record<string, unknown>, family: ToolFamily): NativeView | undefined {
  if (family === "read" && matches(value, {
    path: string, content: string, sha256: string, total_lines: count, truncated: boolean, instructions: string,
    newline_style: string,
    newline_counts: (item) => isRecord(item) && matches(item, { LF: count, CRLF: count, CR: count }),
  })) {
    const counts = value.newline_counts as Record<string, number>;
    const lines = (value.content as string).split("\n");
    const first = /^(\d+):/.exec(lines[0]);
    const last = /^(\d+):/.exec(lines.at(-1) || "");
    const numbered = first && last && Number.isSafeInteger(Number(first[1])) && Number.isSafeInteger(Number(last[1]))
      && Number(first[1]) > 0 && Number(last[1]) >= Number(first[1]);
    return {
      format: "File content", sections: [section("Content", value.content), ...(value.instructions ? [section("Instructions", value.instructions)] : [])],
      metadata: [field("Path", value.path), ...(numbered ? [field("Returned lines", `${first[1]}–${last[1]}`)] : []),
        field("Total lines", value.total_lines)],
      diagnostics: [field("Newline style", value.newline_style),
        field("Newline counts", `LF ${counts.LF} · CRLF ${counts.CRLF} · CR ${counts.CR}`), field("SHA-256", value.sha256)],
      indicators: truncation(value.truncated),
    };
  }
  if ((family === "edit" || family === "write") && matches(value, { path: string, diff: string, changed: boolean })) {
    return { format: "File changes", metadata: [field("Path", value.path)],
      sections: [{ ...section("Unified diff", value.diff), kind: "diff" }],
      indicators: [{ text: value.changed ? "Change reported" : "No change reported", warning: false }] };
  }
  if (family === "list" && matches(value, { paths: strings, truncated: boolean, instructions: string })) {
    const paths = value.paths as string[];
    return { format: "File list", sections: [{ label: "Paths", text: paths.join("\n"), kind: "list" }, ...(value.instructions ? [section("Instructions", value.instructions)] : [])],
      metadata: [field("Paths returned", paths.length)], indicators: truncation(value.truncated) };
  }
  if (family === "search" && matches(value, { matches: strings, truncated: boolean, skipped_files: count })) {
    const found = value.matches as string[];
    return { format: "Search results", sections: [{ label: "Matches", text: found.join("\n"), kind: "list" }],
      metadata: [field("Matches returned", found.length), ...(value.skipped_files ? [field("Skipped files", value.skipped_files)] : [])], indicators: truncation(value.truncated) };
  }
  if (family === "schedule" && matches(value, {
    wakeup_id: string, session_id: string, due_at: string, reason: string, status: (item) => item === "scheduled", root_session_id: string, run_id: string,
  })) {
    return { format: "Scheduling acknowledgement", sections: [section("Reason", value.reason)],
      metadata: [field("Status", value.status), field("Due at", value.due_at), field("Wake-up ID", value.wakeup_id)],
      diagnostics: [field("Session", value.session_id), field("Root session", value.root_session_id), field("Run", value.run_id)],
      note: "Request acknowledged; firing is reported in wake-up activity." };
  }
  return undefined;
}

function inputView(value: Record<string, unknown>, family: ToolFamily): NativeView | undefined {
  if (family === "read" && matches(value, { path: string }, { start_line: positiveCount, limit: positiveCount })) {
    return { format: "File request", metadata: [field("Path", value.path),
      ...(Object.hasOwn(value, "start_line") ? [field("Start line", value.start_line)] : []),
      ...(Object.hasOwn(value, "limit") ? [field("Line limit", value.limit)] : [])], sections: [] };
  }
  if (family === "edit" && matches(value, { path: string, old: string, new: string })) {
    return { format: "Proposed edit", metadata: [field("Path", value.path)],
      sections: [section("Original text to match", value.old), section("Proposed replacement", value.new)] };
  }
  if (family === "write" && matches(value, { path: string, content: string })) {
    return { format: "Proposed file", metadata: [field("Path", value.path)], sections: [section("Proposed file content", value.content)] };
  }
  if (family === "schedule" && matches(value, { reason: string }, { seconds: delay, minutes: delay, hours: delay, days: delay })) {
    const units = [["seconds", 1], ["minutes", 60], ["hours", 3600], ["days", 86400]] as const;
    const total = units.reduce((sum, [unit, scale]) => sum + (typeof value[unit] === "number" ? value[unit] * scale : 0), 0);
    if (!(total > 0 && total <= 604800)) return undefined;
    return { format: "Wake-up request", metadata: [field("Delay", `${total} seconds`)],
      diagnostics: units.filter(([unit]) => Object.hasOwn(value, unit)).map(([unit]) => field(unit, value[unit])),
      sections: [section("Reason", value.reason)], note: "Scheduling request; wake-up activity reports firing." };
  }
  return undefined;
}

// Each adapter accepts the entire known schema or falls back to complete JSON.
// A new backend diagnostic can never disappear inside a familiar-looking view.
export function presentToolValue(text: string, toolName: string, role: "result" | "inputs" = "result"): ResultPresentation {
  const generic = presentToolResult(text, role === "result" ? toolName : "");
  // Inputs have historically been exact literal evidence. Unfamiliar schemas
  // keep that surface; safe JSON token coloring does not change its text.
  const fallback = role === "inputs" ? { ...generic, text } : generic;
  const presentation = toolPresentation(toolName);
  const value = toolObject(text);
  const native = value && (role === "inputs" ? inputView(value, presentation.family) : resultView(value, presentation.family));
  if (native) return { ...fallback, ...native };
  if (presentation.family === "mcp") return { ...fallback,
    metadata: [field("MCP connection", presentation.origin), field("Method", presentation.method)] };
  return fallback;
}
