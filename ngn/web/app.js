const $ = (id) => document.getElementById(id);
let token = "", selected = "", socket, cursor = 0, generation = 0, stateRevision = 0, submitting = false, activeRun, pendingApproval, pendingSubmission;
let taskRevision = 0, inspected, inspectorSocket, inspectorGeneration = 0;
let queueRevision = 0, queueState = { paused: false, entries: [] };
const turns = new Map(), toolCards = new Map();

async function api(path, body) {
  const response = await fetch(path, { method: body === undefined ? "GET" : "POST", headers: { "x-ngn-token": token, "content-type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const text = await response.text();
  let value;
  try { value = JSON.parse(text); } catch { throw new Error(`HTTP ${response.status}: invalid server response`); }
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : `HTTP ${response.status}`);
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
      const card = node("details", "tool"); const summary = node("summary", "", `${record.call.name} · running`); const pre = node("pre", "", JSON.stringify(record.call.arguments, null, 2)); card.append(summary, pre); append(card); toolCards.set(`${record.run_id}/${record.call.call_id}`, { summary, pre }); while (toolCards.size > 1000) toolCards.delete(toolCards.keys().next().value); turn.current = null; break;
    }
    case "tool_completed": {
      const card = toolCards.get(`${record.run_id}/${record.result.call_id}`);
      if (card) { card.summary.textContent = `${record.result.name} · ${record.result.error ? "denied / failed" : "done"} · ${record.duration_ms} ms`; card.pre.textContent += "\n\n" + (record.result.error || JSON.stringify(record.result.result, null, 2)); }
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
}
async function refreshQueue() {
  const revision = ++queueRevision, session = selected;
  const { queue, limit } = await api(`/api/sessions/${session}/queue`);
  if (revision !== queueRevision || session !== selected || queue.session_id !== session) return;
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
  const revision = ++taskRevision, session = selected;
  const { tasks, limits } = await api(`/api/sessions/${session}/tasks`);
  if (revision !== taskRevision || session !== selected) return;
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
  const { sessions } = await api("/api/sessions");
  $("sessions").replaceChildren();
  for (const session of sessions) { const button = node("button", session.id === selected ? "selected" : "", session.title); button.title = session.id; button.onclick = () => select(session.id, session.title); $("sessions").append(button); }
  return sessions;
}
async function refreshState() {
  const revision = ++stateRevision, session = selected;
  const { runs, approvals } = await api("/api/state");
  if (revision !== stateRevision || session !== selected) return;
  activeRun = runs.find((run) => run.session_id === selected);
  pendingApproval = approvals.find((approval) => approval.session_id === selected || approval.root_session_id === selected);
  $("cancel").hidden = !activeRun; $("send").disabled = submitting;
  $("send").textContent = activeRun || queueState.paused ? "Queue ↗" : "Send ↗";
  $("approval").hidden = !pendingApproval;
  if (pendingApproval) { $("approval-label").textContent = `Depth ${pendingApproval.depth || 0} · ${pendingApproval.agent_title || "Main"} · ${pendingApproval.call.name} · allow this exact call once?`; $("preview").textContent = pendingApproval.preview; }
  status(pendingApproval ? "Waiting for your decision" : activeRun ? "Agent working · Esc to stop" : "Ready");
}
async function select(id, title = "New conversation") {
  selected = id; cursor = 0; generation++; const mine = generation;
  closeTask();
  queueRevision++; queueState = { paused: false, entries: [] }; $("queue").replaceChildren(); $("queue-status").textContent = "Loading queue…";
  if (socket) { socket.onclose = null; socket.close(); }
  turns.clear(); toolCards.clear(); $("transcript").replaceChildren(); $("title").textContent = title;
  await refreshSessions(); await refreshState(); await refreshTasks(); await refreshQueue();
  function connect() {
    if (mine !== generation) return;
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events?session_id=${encodeURIComponent(id)}&after=${cursor}`, ["ngn.events.v1", `ngn.token.${token}`]);
    socket.onopen = () => { if (mine !== generation) return; $("connection").textContent = "Connected"; refreshState().catch((error) => status(error.message)); refreshQueue().catch((error) => status(error.message)); };
    socket.onmessage = ({ data }) => { if (mine !== generation) return; try { render(JSON.parse(data)); } catch { status("Invalid event received; reconnect to the host"); } };
    socket.onclose = () => { if (mine !== generation) return; $("connection").textContent = "Reconnecting…"; setTimeout(connect, 1200); };
  }
  connect(); $("prompt").focus();
}
async function createSession() { const { session } = await api("/api/sessions", {}); await select(session.id); }
$("new-session").onclick = () => createSession().catch((error) => status(error.message));
$("composer").onsubmit = async (event) => {
  event.preventDefault(); if (submitting) return;
  const prompt = $("prompt").value; if (!prompt.trim()) return;
  if (!pendingSubmission || pendingSubmission.prompt !== prompt || pendingSubmission.session !== selected) pendingSubmission = { id: crypto.randomUUID(), prompt, session: selected };
  const submission = pendingSubmission;
  submitting = true; $("send").disabled = true;
  try { await api(`/api/sessions/${submission.session}/runs`, { prompt, request_id: submission.id }); if (pendingSubmission === submission) pendingSubmission = null; if (selected === submission.session && $("prompt").value === prompt) $("prompt").value = ""; await refreshState(); await refreshQueue(); await refreshSessions(); }
  catch (error) { status(error.message + " · retrying this message reuses its submission ID"); }
  finally { submitting = false; $("send").disabled = false; }
};
$("prompt").onkeydown = (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("composer").requestSubmit(); } };
async function cancel() { if (activeRun) { await api(`/api/runs/${activeRun.id}/cancel`, {}); status("Stopping; completed actions are not rolled back"); } }
$("cancel").onclick = () => cancel().catch((error) => status(error.message));
document.onkeydown = (event) => { if (event.key === "Escape") cancel().catch((error) => status(error.message)); };
for (const [id, allow] of [["allow", true], ["deny", false]]) $(id).onclick = async () => { const decision = pendingApproval; if (!decision) return; $("allow").disabled = $("deny").disabled = true; try { await api("/api/approvals", { run_id: decision.run_id, approval_id: decision.id, allow }); await refreshState(); } catch (error) { status(error.message); } finally { $("allow").disabled = $("deny").disabled = false; } };

try {
  const bootstrap = await api("/api/bootstrap"); token = bootstrap.token;
  $("version").textContent = bootstrap.version;
  $("workspace").textContent = bootstrap.workspace;
  $("runtime").textContent = `${bootstrap.backend === "demo" ? "Offline Rust demo" : "nagents · " + (bootstrap.model || "provider not configured")}${bootstrap.read_only ? " · read only" : ""}`;
  const sessions = await refreshSessions();
  if (sessions.length) await select(sessions[0].id, sessions[0].title); else await createSession();
} catch (error) { $("connection").textContent = "Disconnected"; status(error.message); }
