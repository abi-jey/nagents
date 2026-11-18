const $ = (id) => document.getElementById(id);
let token = "", selected = "", socket, cursor = 0, generation = 0, stateRevision = 0, submitting = false, activeRun, pendingApproval, pendingSubmission;
let taskRevision = 0, inspected, inspectorSocket, inspectorGeneration = 0;
let queueRevision = 0, queueState = { paused: false, entries: [] };
let sessionRevision = 0, draftRevision = 0;
const steeringStates = new Map(), steeringRevisions = new Map(), steeringAttempts = new Map();
const turns = new Map(), toolCards = new Map();
const pointerMedia = globalThis.matchMedia?.("(hover: none) and (pointer: coarse)");
let touchComposer = Boolean(pointerMedia?.matches);
function composerInputMode() {
  touchComposer = Boolean(pointerMedia?.matches);
  $("prompt").enterKeyHint = touchComposer ? "enter" : "send";
  $("prompt").placeholder = touchComposer ? "Ask about your workspace… Return adds a new line; use Send or Queue" : "Ask about your workspace… Enter to send, Shift+Enter for a new line";
}
pointerMedia?.addEventListener?.("change", composerInputMode);
composerInputMode();
for (const id of ["send", "cancel", "steer"]) $(id).onmousedown = (event) => { if (touchComposer) event.preventDefault(); };
$("prompt").oninput = () => { draftRevision++; renderSteering(); };

export function formatDuration(milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "unknown";
  milliseconds = Math.floor(milliseconds);
  if (milliseconds < 1000) return `${milliseconds}ms`;
  const scale = milliseconds < 10000 ? 100 : 10;
  const rounded = Math.round(milliseconds / (scale === 100 ? 10 : 100));
  if (rounded < 60 * scale) return `${(rounded / scale).toFixed(scale === 100 ? 2 : 1).replace(/\.?0+$/, "")}s`;
  const seconds = Math.round(milliseconds / 1000);
  for (const [size, unit, smaller, smallerUnit] of [[86400, "d", 3600, "h"], [3600, "h", 60, "m"], [60, "m", 1, "s"]]) {
    if (seconds < size) continue;
    const major = Math.floor(seconds / size), minor = Math.floor((seconds % size) / smaller);
    const compact = `${major}${unit}`, text = compact + (minor ? ` ${minor}${smallerUnit}` : "");
    return text.length <= 8 ? text : compact.length <= 8 ? compact : ">999999d";
  }
  return "1m";
}
export function toolTarget(call) {
  const key = ({ shell: "command", skill: "name", read_file: "path", list_files: "path", write: "path", edit: "path", search: "query" })[call.name];
  const value = key && call.arguments?.[key];
  if (typeof value !== "string") return "";
  const text = value.replace(/[\x00-\x1f\x7f-\x9f]/g, " ").trim().replace(/\s+/g, " ");
  const characters = Array.from(text);
  return text ? ` · ${characters.slice(0, 96).join("")}${characters.length > 96 ? "…" : ""}` : "";
}

async function api(path, body) {
  const response = await fetch(path, { method: body === undefined ? "GET" : "POST", headers: { "x-ngn-token": token, "content-type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const text = await response.text();
  let value;
  try { value = JSON.parse(text); } catch { throw new Error(`HTTP ${response.status}: invalid server response`); }
  if (!response.ok) { const error = new Error(typeof value.detail === "string" ? value.detail : `HTTP ${response.status}`); error.status = response.status; throw error; }
  return value;
}
function status(text) { $("status").textContent = text; }
function node(tag, className, text) { const result = document.createElement(tag); if (className) result.className = className; if (text !== undefined) result.textContent = text; return result; }
function append(element) { const transcript = $("transcript"); const nearEnd = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 120; transcript.append(element); while (transcript.children.length > 1000) transcript.firstChild.remove(); if (nearEnd) transcript.scrollTop = transcript.scrollHeight; }
function message(role, text = "") { const article = node("article", `message ${role}`); article.append(node("div", "message-label", role === "user" ? "You" : "ngn")); const paragraph = node("p", "", text); article.append(paragraph); append(article); return paragraph; }
function render(record) {
  if (record.session_id !== selected || record.sequence <= cursor) return;
  cursor = record.sequence;
  const turn = turns.get(record.run_id) || { current: null };
  turns.set(record.run_id, turn);
  while (turns.size > 1000) turns.delete(turns.keys().next().value);
  switch (record.event) {
    case "run_started": message("user", record.prompt); break;
    case "text_delta": if (!turn.current) turn.current = message("assistant"); turn.current.append(document.createTextNode(record.text)); break;
    case "text_completed": if (!turn.current) turn.current = message("assistant"); turn.current.textContent = record.text; turn.current = null; break;
    case "tool_started": {
      const card = node("details", "tool"), hint = toolTarget(record.call); const summary = node("summary", "", `${record.call.name}${hint} · running`); const pre = node("pre", "", JSON.stringify(record.call.arguments, null, 2)); card.append(summary, pre); append(card); toolCards.set(`${record.run_id}/${record.call.call_id}`, { summary, pre, hint, name: record.call.name }); while (toolCards.size > 1000) toolCards.delete(toolCards.keys().next().value); turn.current = null; break;
    }
    case "tool_completed": {
      const card = toolCards.get(`${record.run_id}/${record.result.call_id}`);
      if (card) { card.summary.textContent = `${record.result.name}${card.hint} · ${record.result.error ? "denied / failed" : "done"} · ${formatDuration(record.duration_ms)}`; card.pre.textContent += "\n\n" + (record.result.error || JSON.stringify(record.result.result, null, 2)); }
      break;
    }
    case "tool_approval_timing": {
      const card = toolCards.get(`${record.run_id}/${record.call_id}`);
      if (card) card.summary.textContent = `${card.name}${card.hint} · ${record.waiting ? "waiting for approval" : "running"} · ${formatDuration(record.elapsed_ms)} active`;
      break;
    }
    case "notice": append(node("p", "notice", record.text)); break;
    case "task_started": append(node("p", "notice", `Agent ${record.task.title} started · depth ${record.task.depth}${record.task.read_only ? " · read only" : ""}`)); break;
    case "task_finished": append(node("p", record.error ? "notice error" : "notice", `Agent ${record.task.title}: ${record.task.status}${record.error ? ": " + record.error : ""}`)); break;
    case "run_finished": append(node("p", record.error ? "notice error" : "notice", `${record.status}${record.error ? ": " + record.error : ""}`)); break;
  }
  const transcript = $("transcript");
  if (transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 220) transcript.scrollTop = transcript.scrollHeight;
  if (["run_started", "run_finished", "approval_requested", "approval_resolved", "task_started", "task_updated", "task_finished"].includes(record.event)) refreshState().catch((error) => status(error.message));
  if (["task_started", "task_updated", "task_finished"].includes(record.event)) refreshTasks().catch((error) => status(error.message));
  if (["run_queued", "run_started", "run_finished", "queue_updated"].includes(record.event)) refreshQueue().catch((error) => status(error.message));
  if (["run_started", "steering_updated", "run_finished"].includes(record.event)) refreshSteering(record.run_id, record.session_id).catch((error) => status(error.message));
}

const steeringWords = {
  pending: "pending · admitted, not offered yet",
  offered: "offered · persistence not confirmed",
  applied: "applied · persistence acknowledged, not proof of model consumption",
  uncertain: "uncertain · may be persisted; inspect conversation, no replay",
  undelivered: "undelivered · turn ended before offer; not queued or replayed",
};
function steeringKey(session, prompt) { return `${session}\0${prompt}`; }
function boundSteeringDraft(prompt = $("prompt").value) { return [...steeringAttempts.values()].find((attempt) => attempt.prompt === prompt); }
function capableRoot() {
  const state = steeringStates.get(activeRun?.id);
  return state?.run_id === activeRun?.id && state?.session_id === selected && state?.run_status === "running" && state?.supported && state?.accepting;
}
function renderSteering() {
  $("steer").disabled = !capableRoot() || Boolean(boundSteeringDraft()) || [...steeringAttempts.values()].some((attempt) => attempt.session === selected && attempt.inFlight);
  $("steering-status").textContent = capableRoot() ? `Exact active root ${activeRun.id} · next persisted safe boundary, never between tools` : "Steering unavailable: requires an accepting, capable active root. Send / Enter still uses the next-run FIFO.";
  const receipts = [...steeringStates.values()].filter((state) => state.session_id === selected).flatMap((state) => state.receipts).sort((a, b) => a.position - b.position);
  $("steering-receipts").replaceChildren();
  for (const receipt of receipts) {
    const row = node("article", "steering-receipt");
    row.append(node("strong", "", `#${receipt.position} · ${steeringWords[receipt.status] || "unknown · inspect host state"}`), node("div", "receipt-identity", `Run ${receipt.run_id} · ${receipt.input_id}`), node("pre", "", receipt.prompt));
    $("steering-receipts").append(row);
  }
  $("steering-attempts").replaceChildren();
  for (const attempt of steeringAttempts.values()) {
    if (attempt.session !== selected && attempt.prompt !== $("prompt").value) continue;
    const row = node("article", "steering-receipt");
    row.append(node("strong", "", attempt.receipt ? steeringWords[attempt.receipt.status] : attempt.inFlight ? "Request in flight · admission not confirmed" : attempt.rejected ? "Request rejected · draft retained, never queued" : "Admission unconfirmed · never rebound or queued"), node("div", "receipt-identity", `Session ${attempt.session} · exact run ${attempt.run} · request ${attempt.id}`), node("pre", "", attempt.prompt));
    const retry = node("button", "", "Check / retry exact run"); retry.disabled = attempt.inFlight;
    retry.onclick = () => postSteering(attempt);
    row.append(retry);
    if (attempt.receipt) {
      const clear = node("button", "", "Clear bound draft");
      clear.onclick = () => { if ($("prompt").value === attempt.prompt) { $("prompt").value = ""; draftRevision++; } steeringAttempts.delete(steeringKey(attempt.session, attempt.prompt)); renderSteering(); };
      row.append(clear);
    } else if (attempt.rejected) {
      const release = node("button", "", "Release rejected draft");
      release.onclick = () => { steeringAttempts.delete(steeringKey(attempt.session, attempt.prompt)); renderSteering(); status("Host-rejected binding released locally · draft retained; choose the next action explicitly"); };
      row.append(release);
    }
    $("steering-attempts").append(row);
  }
}
async function refreshSteering(run, session = selected) {
  if (!run || session !== selected) return;
  const mine = generation, revision = (steeringRevisions.get(run) || 0) + 1;
  steeringRevisions.set(run, revision);
  const { steering } = await api(`/api/runs/${run}/steer`);
  if (mine !== generation || session !== selected || steeringRevisions.get(run) !== revision || steering.run_id !== run || steering.session_id !== session) return;
  // Attribution comes only from an exact host snapshot, never a child inspector.
  steering.receipts = steering.receipts.filter((receipt) => receipt.run_id === run && receipt.session_id === session);
  steeringStates.set(run, steering);
  for (const attempt of steeringAttempts.values()) {
    if (attempt.run !== run || attempt.session !== session) continue;
    const receipt = steering.receipts.find((saved) => saved.request_id === attempt.id && saved.prompt === attempt.prompt);
    if (receipt) attempt.receipt = receipt;
  }
  renderSteering();
}
async function postSteering(attempt) {
  if (attempt.inFlight) return;
  const mine = generation, revision = draftRevision;
  attempt.inFlight = true; renderSteering();
  try {
    const { receipt } = await api(`/api/runs/${attempt.run}/steer`, { prompt: attempt.prompt, request_id: attempt.id });
    if (receipt.run_id !== attempt.run || receipt.session_id !== attempt.session || receipt.request_id !== attempt.id || receipt.prompt !== attempt.prompt) throw new Error("Steering response identity mismatch");
    attempt.receipt = receipt;
    if (mine === generation && selected === attempt.session) {
      const state = steeringStates.get(attempt.run);
      // A delayed POST may carry an older status than an event-triggered GET.
      if (state && !state.receipts.some((saved) => saved.input_id === receipt.input_id)) state.receipts.push(receipt);
      if (revision === draftRevision && $("prompt").value === attempt.prompt) { $("prompt").value = ""; draftRevision++; steeringAttempts.delete(steeringKey(attempt.session, attempt.prompt)); }
      else if ($("prompt").value !== attempt.prompt) steeringAttempts.delete(steeringKey(attempt.session, attempt.prompt));
      status(steeringWords[receipt.status] || "Steering receipt saved; inspect exact run");
      await refreshSteering(attempt.run, attempt.session).catch((error) => { if (mine === generation && selected === attempt.session) status(`Steering receipt saved · snapshot unavailable: ${error.message}`); });
    }
  } catch (error) {
    attempt.rejected = error.status === 409;
    if (mine === generation && selected === attempt.session) status(`${error.message} · draft retained; retry only the bound run/UUID, never Queue`);
    if (attempt.rejected && mine === generation && selected === attempt.session) await refreshSteering(attempt.run, attempt.session).catch(() => {});
  } finally { attempt.inFlight = false; renderSteering(); }
}
$("steer").onclick = async () => {
  const prompt = $("prompt").value;
  if (!prompt.trim()) return;
  if (pendingSubmission?.prompt === prompt) { status("This draft has a FIFO submission binding; retry Send in its original conversation, not steering"); return; }
  const key = steeringKey(selected, prompt);
  if (boundSteeringDraft(prompt)) { status("This draft already has an exact run/UUID binding · use Check / retry exact run in receipts, not the active-turn button"); return; }
  if (!capableRoot()) { status("Steering unavailable or closing · draft retained, not queued"); return; }
  const attempt = { id: crypto.randomUUID(), session: selected, run: activeRun.id, prompt, inFlight: false };
  steeringAttempts.set(key, attempt);
  await postSteering(attempt);
};
async function refreshQueue() {
  const revision = ++queueRevision, session = selected, mine = generation;
  const { queue, limit } = await api(`/api/sessions/${session}/queue`);
  if (mine !== generation || revision !== queueRevision || session !== selected || queue.session_id !== session) return;
  queueState = queue;
  $("queue-status").textContent = `${queue.paused ? "Paused · resume explicitly" : "FIFO · runs after the current turn"} · ${queue.entries.length}/${limit || 32} pending`;
  $("pause-queue").textContent = queue.paused ? "Resume queue" : "Pause queue";
  $("clear-queue").disabled = !queue.entries.length;
  $("send").textContent = activeRun || queue.paused ? "Queue ↗" : "Send ↗";
  $("queue").replaceChildren();
  for (const [index, entry] of queue.entries.entries()) {
    const row = node("div", "task-row");
    row.append(node("span", "queued-prompt", `${index + 1}. ${entry.prompt}`));
    const remove = node("button", "", "Remove");
    remove.onclick = async () => {
      if (session !== selected) return;
      remove.disabled = true;
      try { await api(`/api/runs/${entry.run.id}/cancel`, {}); if (session === selected) await refreshQueue(); }
      catch (error) { status(error.message); }
      finally { remove.disabled = false; }
    };
    row.append(remove); $("queue").append(row);
  }
}
async function controlQueue(action) {
  const session = selected;
  await api(`/api/sessions/${session}/queue`, { action });
  if (session === selected) await refreshQueue();
}
$("pause-queue").onclick = () => controlQueue(queueState.paused ? "resume" : "pause").catch((error) => status(error.message));
$("clear-queue").onclick = () => controlQueue("clear").catch((error) => status(error.message));
function closeTask() { inspectorGeneration++; inspected = undefined; if (inspectorSocket) { inspectorSocket.onclose = null; inspectorSocket.close(); } $("task-inspector").hidden = true; }
function inspectTask(task) {
  closeTask(); inspected = task; $("task-inspector").hidden = false; $("task-title").textContent = `${task.title} · ${task.status} · depth ${task.depth}${task.read_only ? " · read only" : ""}`; $("cancel-task").hidden = task.status !== "running"; $("task-transcript").textContent = "";
  const mine = inspectorGeneration;
  let after = 0, streamed = false, text = "";
  function connect() {
    if (mine !== inspectorGeneration || inspected?.run_id !== task.run_id) return;
    inspectorSocket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events?session_id=${encodeURIComponent(task.session_id)}&after=${after}`, ["ngn.events.v1", `ngn.token.${token}`]);
    inspectorSocket.onmessage = ({ data }) => {
      if (mine !== inspectorGeneration || inspected?.run_id !== task.run_id) return;
      try {
        const record = JSON.parse(data); if (record.session_id !== task.session_id || record.run_id !== task.run_id || record.sequence <= after) return; after = record.sequence;
        if (record.event === "run_started") text += `ASSIGNMENT\n${record.prompt}\n\n`;
        else if (record.event === "text_delta") { text += record.text; streamed = true; }
        else if (record.event === "text_completed") { if (!streamed) text += record.text; text += "\n"; streamed = false; }
        else if (record.event === "tool_started") { text += `\n[${record.call.name}]\n`; streamed = false; }
        else if (record.event === "tool_completed") text += record.result.error ? `[${record.result.error}]\n` : `[${record.result.name}: done]\n`;
        else if (record.event === "run_finished") { text += `\n${record.status}${record.error ? ": " + record.error : ""}\n`; $("cancel-task").hidden = true; }
        text = text.slice(-256000); $("task-transcript").textContent = text;
      } catch { status("Invalid child event received"); }
    };
    inspectorSocket.onclose = () => { if (mine === inspectorGeneration && inspected?.run_id === task.run_id) setTimeout(connect, 1200); };
  }
  connect();
}
async function stopTask(task) { await api(`/api/runs/${task.run_id}/cancel`, {}); status(`Stopping agent ${task.title}`); }
async function refreshTasks() {
  const revision = ++taskRevision, session = selected, mine = generation;
  const { tasks, limits } = await api(`/api/sessions/${session}/tasks`);
  if (mine !== generation || revision !== taskRevision || session !== selected) return;
  $("tasks").replaceChildren(); $("tasks-panel").hidden = !tasks.length;
  if (limits) $("task-limits").textContent = `Per root run: ${limits.max_concurrent} concurrent · ${limits.max_executions} executions · depth ${limits.max_depth}`;
  for (const task of tasks) {
    if (task.root_session_id !== session && task.parent_session_id !== session) continue;
    const row = node("div", "task-row"), label = node("span", "", `${"↳ ".repeat(Math.min(task.depth, 8))}${task.title} · ${task.status}${task.read_only ? " · read only" : ""}`);
    const inspect = node("button", "", "Inspect"); inspect.onclick = () => inspectTask(task); row.append(label, inspect);
    if (task.status === "running") { const cancel = node("button", "", "Stop"); cancel.onclick = () => stopTask(task).catch((error) => status(error.message)); row.append(cancel); }
    $("tasks").append(row);
    if (inspected?.run_id === task.run_id) { inspected = task; $("task-title").textContent = `${task.title} · ${task.status} · depth ${task.depth}`; $("cancel-task").hidden = task.status !== "running"; }
  }
}
$("close-task").onclick = closeTask;
$("cancel-task").onclick = () => { if (inspected) stopTask(inspected).catch((error) => status(error.message)); };
async function refreshSessions() {
  const revision = ++sessionRevision, sessionId = selected, mine = generation;
  const { sessions } = await api("/api/sessions");
  if (mine !== generation || revision !== sessionRevision || sessionId !== selected) return sessions;
  $("sessions").replaceChildren(); $("mobile-sessions").replaceChildren();
  for (const session of sessions) {
    if (session.id === selected) $("title").textContent = session.title;
    const button = node("button", session.id === selected ? "selected" : "", session.title); button.title = session.id; button.onclick = () => select(session.id, session.title).catch((error) => status(error.message)); $("sessions").append(button);
    const option = node("option", "", session.title); option.value = session.id; $("mobile-sessions").append(option);
  }
  $("mobile-sessions").value = selected;
  return sessions;
}
async function refreshState() {
  const revision = ++stateRevision, session = selected, mine = generation;
  const { runs, approvals } = await api("/api/state");
  if (mine !== generation || revision !== stateRevision || session !== selected) return;
  activeRun = runs.find((run) => run.session_id === selected);
  pendingApproval = approvals.find((approval) => approval.session_id === selected || approval.root_session_id === selected);
  $("cancel").hidden = !activeRun; $("send").disabled = submitting;
  $("send").textContent = activeRun || queueState.paused ? "Queue ↗" : "Send ↗";
  $("approval").hidden = !pendingApproval;
  if (pendingApproval) { $("approval-label").textContent = `Depth ${pendingApproval.depth || 0} · ${pendingApproval.agent_title || "Main"} · ${pendingApproval.call.name} · allow this exact call once?`; $("preview").textContent = pendingApproval.preview; }
  status(pendingApproval ? "Waiting for your decision" : activeRun ? "Agent working · Esc to stop" : "Ready");
  renderSteering();
  if (activeRun) await refreshSteering(activeRun.id, session);
}
async function select(id, title = "New conversation") {
  selected = id; cursor = 0; generation++; const mine = generation;
  activeRun = undefined; renderSteering();
  pendingApproval = undefined; $("approval").hidden = true; $("cancel").hidden = true;
  closeTask();
  queueRevision++; queueState = { paused: false, entries: [] }; $("queue").replaceChildren(); $("queue-status").textContent = "Loading queue…";
  if (socket) { socket.onclose = null; socket.close(); }
  turns.clear(); toolCards.clear(); $("transcript").replaceChildren(); $("title").textContent = title;
  await refreshSessions(); if (mine !== generation) return;
  await Promise.all([refreshState(), refreshTasks(), refreshQueue()]); if (mine !== generation) return;
  function connect() {
    if (mine !== generation) return;
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events?session_id=${encodeURIComponent(id)}&after=${cursor}`, ["ngn.events.v1", `ngn.token.${token}`]);
    socket.onopen = () => { if (mine !== generation) return; $("connection").textContent = "Connected"; refreshState().catch((error) => status(error.message)); refreshQueue().catch((error) => status(error.message)); for (const state of steeringStates.values()) if (state.session_id === selected) refreshSteering(state.run_id).catch((error) => status(error.message)); };
    socket.onmessage = ({ data }) => { if (mine !== generation) return; try { render(JSON.parse(data)); } catch { status("Invalid event received; reconnect to the host"); } };
    socket.onclose = () => { if (mine !== generation) return; $("connection").textContent = "Reconnecting…"; setTimeout(connect, 1200); };
  }
  connect(); $("prompt").focus();
}
async function createSession() { const { session } = await api("/api/sessions", {}); await select(session.id); }
$("new-session").onclick = () => createSession().catch((error) => status(error.message));
$("mobile-new-session").onclick = $("new-session").onclick;
$("mobile-sessions").onchange = () => select($("mobile-sessions").value).catch((error) => status(error.message));
$("composer").onsubmit = async (event) => {
  event.preventDefault(); if (submitting) return;
  const prompt = $("prompt").value; if (!prompt.trim()) return;
  if (boundSteeringDraft(prompt)) { status("This draft is bound to an exact steering run/UUID · inspect or retry its receipt; not converted to Queue"); return; }
  if (pendingSubmission?.prompt === prompt && pendingSubmission.session !== selected) { status("This FIFO draft is bound to its original conversation · return there to retry; not rebound"); return; }
  if (!pendingSubmission || pendingSubmission.prompt !== prompt || pendingSubmission.session !== selected) pendingSubmission = { id: crypto.randomUUID(), prompt, session: selected };
  const submission = pendingSubmission;
  const mine = generation, revision = draftRevision;
  submitting = true; $("send").disabled = true;
  try { await api(`/api/sessions/${submission.session}/runs`, { prompt, request_id: submission.id }); if (pendingSubmission === submission) pendingSubmission = null; if (mine === generation && selected === submission.session) { if (revision === draftRevision && $("prompt").value === prompt) { $("prompt").value = ""; draftRevision++; } await Promise.all([refreshState(), refreshQueue(), refreshSessions()]); } }
  catch (error) { if (mine === generation && selected === submission.session) status(error.message + " · retrying this message reuses its submission ID"); }
  finally { submitting = false; $("send").disabled = false; }
};
$("prompt").onkeydown = (event) => {
  if (event.isComposing) return;
  // Touch Return belongs to the text editor, not network admission. Buttons and
  // explicit Ctrl/Meta+Enter retain a deliberate send gesture.
  if (touchComposer && !event.ctrlKey && !event.metaKey) return;
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("composer").requestSubmit(); }
};
async function cancel() { if (activeRun) { await api(`/api/runs/${activeRun.id}/cancel`, {}); status("Stopping; completed actions are not rolled back"); } }
$("cancel").onclick = () => cancel().catch((error) => status(error.message));
document.onkeydown = (event) => { if (event.key === "Escape") cancel().catch((error) => status(error.message)); };
for (const [id, allow] of [["allow", true], ["deny", false]]) $(id).onclick = async () => { const decision = pendingApproval; if (!decision) return; $("allow").disabled = $("deny").disabled = true; try { await api("/api/approvals", { run_id: decision.run_id, approval_id: decision.id, allow }); await refreshState(); } catch (error) { status(error.message); } finally { $("allow").disabled = $("deny").disabled = false; } };

try {
  const bootstrap = await api("/api/bootstrap"); token = bootstrap.token;
  $("version").textContent = bootstrap.version;
  $("workspace").textContent = bootstrap.workspace;
  $("runtime").textContent = `nagents · ${bootstrap.model || "provider not configured"}${bootstrap.read_only ? " · read only" : ""}`;
  const sessions = await refreshSessions();
  if (sessions.length) await select(sessions[0].id, sessions[0].title); else await createSession();
} catch (error) { $("connection").textContent = "Disconnected"; status(error.message); }
