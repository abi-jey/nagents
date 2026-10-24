import { appendEvent, type Entry } from "../../../src/features/chat/transcript.js";
import type { WireEvent } from "../../../src/types.js";
export { reasoningParts } from "../../../src/features/chat/reasoningSummary.js";

export type TimedEvent = { at: number; label: string; event: WireEvent };
type Actor = { session_id: string; parent_session_id: string; title: string; depth: number; activation: number };
const explorer: Actor = { session_id: "s1", parent_session_id: "", title: "Explorer", depth: 1, activation: 0 };
export const demoAgents = { s1: { name: "Explorer", parent: "Assistant", provider: "OpenAI", model: "gpt-6-luna", prompt: "Locate attachment styles and report their narrow-screen behavior." } };
export const DURATION = 36;
const run = "image-change", session = "chat-flow-demo";
const events: TimedEvent[] = [];
const summaries = new Map<string, string>();
function add(at: number, label: string, event: WireEvent) { events.push({ at, label, event: { root_session_id: session, run_id: run, ...event } }); }
function summary(at: number, id: string, revision: number, value: string, status = "thinking", runId = run, actor?: Actor) {
  summaries.set(id, value);
  add(at, status === "thinking" ? "Reasoning arrives" : "Reasoning complete", {
    event: "response_metadata", response_id: id, response_session_id: actor ? actor.session_id : session, run_id: runId, ...actor,
    revision, round_number: 1, history_id: "", status, summary: value,
    summary_truncated: false, summary_expired: false, usage_reported: false,
  });
}
function reasoning(start: number, end: number, id: string, title: string, body: string, runId = run, actor?: Actor) {
  summary(start, id, 1, "", "thinking", runId, actor);
  summary(start + .45, id, 2, `**${title}**`, "thinking", runId, actor);
  const words = body.split(" ");
  const batches = Math.ceil(words.length / 4);
  for (let i = 1; i <= batches; i++) summary(start + .6 + (end - start - .8) * i / batches,
    id, i + 2, `**${title}**\n\n${words.slice(0, i * 4).join(" ")}`, "thinking", runId, actor);
  summary(end, id, batches + 3, `**${title}**\n\n${body}`, "responding", runId, actor);
}
function message(start: number, end: number, id: string, value: string, runId = run, actor?: Actor, completeAt = end + .01) {
  const words = value.split(" ");
  for (let i = 0; i < words.length; i += 3) add(start + (end - start) * i / words.length, "Answer streams", {
    event: "text_chunk", response_id: id, chunk: (i ? " " : "") + words.slice(i, i + 3).join(" "), run_id: runId, ...actor,
  });
  add(end, "Answer complete", { event: "text_done", response_id: id, text: value, run_id: runId, ...actor });
  summary(completeAt, id, 100, summaries.get(id) || "", "completed", runId, actor);
}
function call(start: number, ready: number, end: number, index: number, name: string, args: Record<string, string | number>, result: object, actor?: Actor) {
  const raw = JSON.stringify(args, null, 2), generation = `generation-${index}`, id = `call-${index}`;
  const identity = { generation_id: generation, index: 0, id, name, ...actor };
  for (let i = 0; i <= 7; i++) add(start + (ready - start) * i / 8, "Tool arguments arrive", {
    event: "tool_call_progress", ...identity, status: "streaming", arguments_text: raw.slice(0, Math.max(1, Math.floor(raw.length * i / 7))),
  });
  add(ready, "Tool call ready", { event: "tool_call", ...identity, arguments: args });
  add(ready + .2, "Tool starts", { event: "tool_execution_started", ...identity });
  if (name === "shell") {
    add(ready + 1, "Tool output streams", { event: "tool_output", ...identity, text: " RUN  attachment.test.tsx\n\n" });
    add(ready + 2.1, "Tool output streams", { event: "tool_output", ...identity, text: " ✓ image is visible in chat\n ✓ download uses the original file\n" });
    add(ready + 3.1, "Tool output streams", { event: "tool_output", ...identity, text: " ✓ narrow layout wraps\n ✓ keyboard focus stays visible\n" });
  }
  add(end, "Tool result arrives", { event: "tool_result", ...identity, result, duration_ms: (end - ready) * 1000 });
}

add(.5, "User message arrives", { event: "user_message", message_id: "image-request", history_id: "1", text: "Make delivered images visible in chat and keep a download action." });
reasoning(1, 4, "inspect", "Check the rendering path", "I’ll inspect the attachment component and keep original downloads separate from the displayed image. Then I’ll check the mobile layout.");
call(4.3, 6, 7.5, 1, "read_file", { path: "src/features/chat/ChatMedia.tsx" }, { content: "The image currently sits behind a preview action. The original file is already available for download." });
call(4.8, 6.4, 6.8, 5, "to_agent", { agent: "mini", title: "Explorer", prompt: demoAgents.s1.prompt }, { session_id: "s1", status: "running" });
add(6.7, "Explorer is spawned", { event: "task_started", ...explorer, title: "Explorer", origin_history_id: "1", prompt: demoAgents.s1.prompt, trigger: "delegation", provider: "openai", model: "gpt-6-luna" });
reasoning(6.9, 7.8, "explore-plan", "Locate the shared styles", "I’ll find the attachment styles and check whether narrow screens already have a layout rule.", run, explorer);
message(7.9, 8.2, "explore-plan", "I’ll check the shared attachment styles.", run, explorer, 9.3);
call(8.4, 9.2, 10.2, 2, "find", { pattern: "**/attachment*.css" }, { paths: ["src/features/chat/attachment.css"] }, explorer);
message(10.4, 11.4, "explore-result", "Found attachment.css. The narrow-screen rule already wraps the footer; preserve it when moving the download action.", run, explorer);
add(11.5, "Explorer returns its result", { event: "task_completed", ...explorer, title: "Explorer", status: "completed", result: "Found the shared attachment styles and the existing narrow-screen wrapping rule." });
add(11.6, "Result is delivered to Assistant", { event: "task_notification", notification_id: "explorer-return", source_session_id: "s1", source_title: "Explorer", recipient_session_id: "", recipient_title: "Assistant", cause: "completion", text: "Found attachment.css. Preserve its narrow-screen footer wrapping rule." });
summary(6.8, "inspect", 100, summaries.get("inspect")!, "completed");
reasoning(8.5, 10.6, "plan", "Keep the image in the conversation", "The original file and the displayed image can share a compact footer. I’ll keep the filename wrapping and make the download action easy to reach.");
message(10.8, 12, "plan", "I found the image renderer. I’ll keep the image visible and group the filename with its download action.", run, undefined, 14.3);
call(12.2, 14.2, 16.6, 3, "edit", { path: "src/features/chat/ChatMedia.tsx", old: "<PreviewButton file={file} />", new: "<InlineImage file={file} />\n<DownloadButton file={file} />" }, { changed: true, path: "src/features/chat/ChatMedia.tsx", diff: "- PreviewButton\n+ InlineImage\n+ DownloadButton" });
add(14.8, "Follow-up arrives while the tool runs", { event: "user_message", run_id: "", message_id: "keyboard-note", text: "Keep keyboard focus visible too.", queued: true });
call(17, 18.5, 23, 4, "shell", { command: "npm run test:media", timeout: 60 }, { exit_code: 0, stdout: "12 tests passed. Inline images, original downloads, narrow layout and visible keyboard focus are covered." });
reasoning(23.4, 25.5, "finish", "Check the result", "The image stays in chat and the download still uses the original attachment. The focused tests passed, including narrow screens and keyboard focus.");
message(25.7, 28.4, "finish", "Images now appear directly in the conversation. The **download button** stays beside the filename, and the layout wraps on smaller screens.\n\nThe focused attachment tests passed.");
add(28.6, "First response finishes", { event: "run_finished", status: "completed" });
add(29, "Queued follow-up is picked up", { event: "input_admitted", run_id: "keyboard-followup", message_id: "keyboard-note" });
add(29.1, "Follow-up is saved", { event: "user_message", run_id: "keyboard-followup", message_id: "keyboard-note", history_id: "2", text: "Keep keyboard focus visible too." });
reasoning(29.4, 31.6, "keyboard", "Confirm keyboard focus", "The test output already covers visible keyboard focus. I can answer the follow-up from that result without rerunning the tools.", "keyboard-followup");
message(32, 35.2, "keyboard", "Keyboard focus stays visible on the download button. That check passed in the same test run.", "keyboard-followup");
add(35.5, "Conversation is ready", { event: "run_finished", run_id: "keyboard-followup", status: "completed" });

export const timeline: readonly TimedEvent[] = events.sort((a, b) => a.at - b.at);
export const chapters = [
  { at: 0, label: "Start" }, { at: 1, label: "Reasoning" }, { at: 4.3, label: "Arguments" },
  { at: 6.7, label: "Agent spawn" }, { at: 8.4, label: "Parallel work" }, { at: 11.6, label: "Agent result" },
  { at: 12.2, label: "Edit" }, { at: 14.8, label: "Follow-up queued" },
  { at: 19.5, label: "Streamed output" }, { at: 25.7, label: "Answer" }, { at: 29, label: "Next turn" }, { at: DURATION, label: "Final reply" },
] as const;

export type Frame = { time: number; entries: Entry[]; eventCount: number; latest?: TimedEvent };
export function frameAt(value: number): Frame {
  const time = Math.max(0, Math.min(DURATION, Number.isFinite(value) ? value : 0));
  let entries: Entry[] = [], eventCount = 0, latest: TimedEvent | undefined;
  for (const item of timeline) {
    if (item.at > time) break;
    entries = appendEvent(entries, item.event);
    eventCount++; latest = item;
  }
  return { time, entries, eventCount, latest };
}
export function nextTime(time: number): number { return timeline.find(item => item.at > time + .0001)?.at ?? DURATION; }
