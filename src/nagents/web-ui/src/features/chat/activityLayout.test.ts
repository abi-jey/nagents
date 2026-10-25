import assert from "node:assert/strict";
import test from "node:test";
import { appearancePresets, defaultAppearance } from "../settings/appearance.js";
import { ActivityLayout, activityCategory, activityLayout, activityState, importantActivity, visibleTranscriptItems } from "./activityLayout.js";
import type { Entry, TranscriptItem } from "./transcript.js";
import type { ResponseMetadata } from "./responseMetadata.js";

const entry = (id: string, kind: Entry["kind"], text = "", extra: Partial<Entry> = {}): Entry => ({ id, kind, text, ...extra });
const item = (entry: Entry): TranscriptItem => ({ kind: "entry", entry });

test("Compact groups only adjacent activity with the same category and execution scope", () => {
  const first = item(entry("shell-one", "tool", "", { title: "run_shell_command", runId: "run" }));
  const second = item(entry("shell-two", "tool", "", { title: "shell", runId: "run" }));
  const third = item(entry("shell-child", "tool", "", { title: "shell", runId: "run", taskId: "child" }));
  const separated = activityLayout([first, second, third], defaultAppearance);
  assert.equal(separated[0].kind, "activity_group");
  if (separated[0].kind === "activity_group") {
    assert.equal(separated[0].category, "shell");
    assert.deepEqual(separated[0].items, [first, second]);
  }
  assert.equal(separated[1].kind, "activity_group");
  if (separated[1].kind === "activity_group") assert.deepEqual(separated[1].items, [third]);

  const tools = item(entry("tool", "tool", "", { title: "mcp__local__lookup", runId: "run" }));
  const interrupted = activityLayout([first, tools, second], defaultAppearance);
  assert.deepEqual(interrupted.map(value => value.kind), ["activity_group", "activity_group", "activity_group"]);
});

test("response entries with no visible text or reasoning do not split adjacent activity", () => {
  const firstEntry = entry("shell-one", "tool", "", { title: "shell", runId: "run" });
  const first = item(firstEntry);
  const hiddenEntry = entry("hidden-response", "assistant", "", { runId: "run", responseId: "round-two", responseMetadata: {
    response_id: "round-two", response_session_id: "root", run_id: "run", revision: 1, round_number: 1,
    history_id: "", status: "completed", summary: "", summary_truncated: false, summary_expired: false,
    usage_reported: true,
    usage: { prompt_tokens: 12, completion_tokens: 4, total_tokens: 16, reasoning_tokens: 0,
      prompt_reported: true, completion_reported: true, total_reported: true, reasoning_reported: false,
      reasoning_in_output: "unknown" },
  } });
  const hiddenResponse = item(hiddenEntry);
  const secondEntry = entry("shell-two", "tool", "", { title: "shell", runId: "run" });
  const second = item(secondEntry);

  const result = activityLayout([first, hiddenResponse, second], defaultAppearance);
  assert.equal(result.length, 1);
  assert.equal(result[0].kind, "activity_group");
  if (result[0].kind === "activity_group") assert.deepEqual(result[0].items, [first, second]);

  const layout = new ActivityLayout();
  const live = layout.render([first, hiddenResponse, second], defaultAppearance);
  assert.equal(live.length, 1);
  if (live[0].kind === "activity_group") assert.equal(live[0].items.length, 2);

  const visibleResponse = item({ ...hiddenEntry, responseMetadata: { ...hiddenEntry.responseMetadata!, revision: 2, summary: "A plan." } });
  const withReasoning = layout.render([first, visibleResponse, second], defaultAppearance);
  assert.deepEqual(withReasoning.map(value => value.kind), ["activity_group", "entry", "activity_group"]);
  if (withReasoning[0].kind === "activity_group" && withReasoning[2].kind === "activity_group") {
    assert.deepEqual(withReasoning[0].items, [first]);
    assert.deepEqual(withReasoning[2].items, [second]);
  }

  const hiddenReasoning = { ...defaultAppearance, activity: {
    ...defaultAppearance.activity, reasoning: { ...defaultAppearance.activity.reasoning, placement: "hidden" as const },
  } };
  const withoutReasoning = layout.render([first, visibleResponse, second], hiddenReasoning);
  assert.equal(withoutReasoning.length, 1);
  if (withoutReasoning[0].kind === "activity_group") assert.deepEqual(withoutReasoning[0].items, [first, second]);

  const saved = layout.render([
    item({ ...firstEntry, recorded: true, historyId: "saved-first" }),
    hiddenResponse,
    item({ ...secondEntry, recorded: true, historyId: "saved-second" }),
  ], defaultAppearance);
  assert.equal(saved.length, 1);
  if (saved[0].kind === "activity_group") assert.equal(saved[0].items.length, 2);
});

test("assistant visibility keeps readable content and media without retaining expired or empty reasoning", () => {
  const metadata: ResponseMetadata = { response_id: "response", response_session_id: "root", revision: 1,
    round_number: 0, history_id: "", status: "thinking", summary: "A readable plan.", summary_truncated: false,
    summary_expired: false, usage_reported: false };
  const responseEntry = entry("response", "assistant", "", { responseMetadata: metadata });
  const response = item(responseEntry);
  const layout = new ActivityLayout();
  assert.deepEqual(layout.render([response], defaultAppearance), [response]);
  for (const summary of ["", " \n\t", "## \n** **"]) {
    const empty = item({ ...responseEntry, text: " \n", responseMetadata: { ...metadata, revision: 2, summary } });
    assert.deepEqual(layout.render([empty], defaultAppearance), []);
    assert.deepEqual(activityLayout([empty], defaultAppearance), []);
  }
  const expired = item({ ...responseEntry, responseMetadata: { ...metadata, summary_expired: true } });
  assert.deepEqual(layout.render([expired], defaultAppearance), []);
  const upload = item(entry("upload", "assistant", "", { uploads: [
    { upload_id: "upload", filename: "report.pdf", media_type: "application/pdf", byte_length: 1024 },
  ] }));
  const caption = item(entry("caption", "assistant", "", { liveCaption: {
    sessionId: "voice", seq: 1, speaker: "assistant", start: 0, end: 100, anchorHistoryId: "",
  } }));
  assert.deepEqual(visibleTranscriptItems([upload, caption], defaultAppearance), [upload, caption]);
  const messages = appearancePresets.find(preset => preset.id === "messages")!.value;
  for (const status of ["failed", "cancelled"] as const) {
    const interrupted = item({ ...responseEntry, responseMetadata: { ...metadata, status, summary: "" } });
    assert.deepEqual(visibleTranscriptItems([interrupted], messages), [interrupted]);
  }
});

test("hidden activity is omitted but errors, warnings, and approval work stay visible", () => {
  const messages = appearancePresets.find(preset => preset.id === "messages")!.value;
  const ordinary = item(entry("ordinary", "tool", "", { title: "shell", state: "Completed" }));
  const failed = item(entry("failed", "tool", "", { title: "shell", state: "Failed", error: "exit 3" }));
  const warning = item(entry("warning", "status", "Workspace changed", { level: "warning" }));
  const approval = item(entry("approval", "tool", "", { title: "shell", state: "Waiting for approval" }));
  const denied = item(entry("denied", "tool", "", { title: "shell", state: "Approval denied" }));
  const interrupted = item(entry("interrupted", "tool", "", { title: "shell", state: "Interrupted" }));
  const missing = item(entry("missing", "tool", "", { title: "shell", state: "No result recorded" }));
  const result = activityLayout([ordinary, failed, warning, approval, denied, interrupted, missing], messages);
  assert.deepEqual(result.map(value => value.kind === "entry" ? value.entry.id : value.kind === "thread" ? value.thread.id : value.id),
    ["failed", "warning", "approval", "denied", "interrupted", "missing"]);
  assert.equal(importantActivity(failed), true);
  assert.equal(importantActivity(warning), true);
  assert.equal(importantActivity(approval), true);
  assert.equal(activityState([failed, warning, approval]), "Needs attention");
  assert.equal(activityState([item(entry("queued", "tool", "", { title: "shell", state: "Requested" }))]), "Running");
  assert.equal(activityState([item(entry("done", "tool", "", { title: "shell", state: "Completed" }))]), "Done");
  assert.equal(activityState([item(entry("unknown", "tool", "", { title: "shell", state: "Recorded result" }))]), "");
  assert.equal(activityCategory(ordinary), "shell");
});

test("run usage metadata does not add a duplicate item to the conversation layout", () => {
  const runUsage = {
    prompt_tokens: 4, completion_tokens: 3, total_tokens: 7, reasoning_tokens: 1,
    prompt_reported: true, completion_reported: true, total_reported: true, reasoning_reported: true,
    reasoning_in_output: "included" as const, response_count: 2, usage_response_count: 2,
    reasoning_response_count: 2, prompt_response_count: 2, completion_response_count: 2,
    total_response_count: 2, complete: true,
  };
  const response: ResponseMetadata = { response_id: "response", response_session_id: "root", run_id: "run",
    revision: 1, round_number: 0, history_id: "answer", status: "completed", summary: "Summary",
    summary_truncated: false, summary_expired: false, usage_reported: true, run_usage: runUsage };
  const answer = item(entry("answer", "assistant", "Answer", { runId: "run", responseId: "response", responseMetadata: response }));
  const rootTool = item(entry("root-tool", "tool", "", { title: "shell", runId: "run" }));
  const childTool = item(entry("child-tool", "tool", "", { title: "shell", runId: "run", taskId: "worker" }));
  const presentation = activityLayout([answer, rootTool, childTool], defaultAppearance);
  assert.deepEqual(presentation.map(value => value.kind), ["entry", "activity_group", "activity_group"]);
  assert.equal(presentation[0], answer);
  assert.equal(response.run_usage?.total_tokens, 7);
  assert.equal((presentation[1].kind === "activity_group" && presentation[1].items[0]), rootTool);
  assert.equal((presentation[2].kind === "activity_group" && presentation[2].items[0]), childTool);
});
