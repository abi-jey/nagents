const $ = (id) => document.getElementById(id);
let token = "", selected = "", socket, cursor = 0, generation = 0, stateRevision = 0, submitting = false, activeRun, pendingApproval, pendingSubmission;
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
    case "run_finished": append(node("p", record.error ? "notice error" : "notice", `${record.status}${record.error ? ": " + record.error : ""}`)); break;
  }
  const transcript = $("transcript");
  if (transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 220) transcript.scrollTop = transcript.scrollHeight;
  if (["run_started", "run_finished", "approval_requested", "approval_resolved"].includes(record.event)) refreshState().catch((error) => status(error.message));
}
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
  pendingApproval = approvals.find((approval) => approval.session_id === selected);
  $("cancel").hidden = !activeRun; $("send").disabled = Boolean(activeRun) || submitting;
  $("approval").hidden = !pendingApproval;
  if (pendingApproval) { $("approval-label").textContent = `${pendingApproval.call.name} · allow this exact call once?`; $("preview").textContent = pendingApproval.preview; }
  status(pendingApproval ? "Waiting for your decision" : activeRun ? "Agent working · Esc to stop" : "Ready");
}
async function select(id, title = "New conversation") {
  selected = id; cursor = 0; generation++; const mine = generation;
  if (socket) { socket.onclose = null; socket.close(); }
  turns.clear(); toolCards.clear(); $("transcript").replaceChildren(); $("title").textContent = title;
  await refreshSessions(); await refreshState();
  function connect() {
    if (mine !== generation) return;
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events?session_id=${encodeURIComponent(id)}&after=${cursor}`, ["ngn.events.v1", `ngn.token.${token}`]);
    socket.onopen = () => { $("connection").textContent = "Connected"; refreshState().catch((error) => status(error.message)); };
    socket.onmessage = ({ data }) => { if (mine !== generation) return; try { render(JSON.parse(data)); } catch { status("Invalid event received; reconnect to the host"); } };
    socket.onclose = () => { if (mine !== generation) return; $("connection").textContent = "Reconnecting…"; setTimeout(connect, 1200); };
  }
  connect(); $("prompt").focus();
}
async function createSession() { const { session } = await api("/api/sessions", {}); await select(session.id); }
$("new-session").onclick = () => createSession().catch((error) => status(error.message));
$("composer").onsubmit = async (event) => {
  event.preventDefault(); if (activeRun || submitting) return;
  const prompt = $("prompt").value; if (!prompt.trim()) return;
  if (!pendingSubmission || pendingSubmission.prompt !== prompt || pendingSubmission.session !== selected) pendingSubmission = { id: crypto.randomUUID(), prompt, session: selected };
  submitting = true; $("send").disabled = true;
  try { await api(`/api/sessions/${selected}/runs`, { prompt, request_id: pendingSubmission.id }); pendingSubmission = null; $("prompt").value = ""; await refreshState(); await refreshSessions(); }
  catch (error) { status(error.message + " · retrying this message reuses its submission ID"); }
  finally { submitting = false; $("send").disabled = Boolean(activeRun); }
};
$("prompt").onkeydown = (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("composer").requestSubmit(); } };
async function cancel() { if (activeRun) { await api(`/api/runs/${activeRun.id}/cancel`, {}); status("Stopping; completed actions are not rolled back"); } }
$("cancel").onclick = () => cancel().catch((error) => status(error.message));
document.onkeydown = (event) => { if (event.key === "Escape") cancel().catch((error) => status(error.message)); };
for (const [id, allow] of [["allow", true], ["deny", false]]) $(id).onclick = async () => { const decision = pendingApproval; if (!decision) return; $("allow").disabled = $("deny").disabled = true; try { await api("/api/approvals", { run_id: decision.run_id, approval_id: decision.id, allow }); await refreshState(); } catch (error) { status(error.message); } finally { $("allow").disabled = $("deny").disabled = false; } };

try {
  const bootstrap = await api("/api/bootstrap"); token = bootstrap.token;
  $("workspace").textContent = bootstrap.workspace;
  $("runtime").textContent = `${bootstrap.backend === "demo" ? "Offline Rust demo" : "nagents · " + (bootstrap.model || "provider not configured")}${bootstrap.read_only ? " · read only" : ""}`;
  const sessions = await refreshSessions();
  if (sessions.length) await select(sessions[0].id, sessions[0].title); else await createSession();
} catch (error) { $("connection").textContent = "Disconnected"; status(error.message); }
