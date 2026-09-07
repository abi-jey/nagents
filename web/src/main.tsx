import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  appendEvent,
  preview,
  readEvents,
  text,
  type Entry,
  type WireEvent,
} from "./protocol";
import "./style.css";

type Bootstrap = {
  token: string;
  workspace: string;
  provider: string;
  model: string;
  agent: string;
  demo: boolean;
  active_run_id: string;
};
type Session = { id: string; title: string; updated_at: string };
type Snapshot = {
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
type Approval = {
  run_id: string;
  approval_id: string;
  call_id: string;
  tool: string;
  description: string;
  preview: string;
  arguments: unknown;
};

async function request(
  path: string,
  token = "",
  body?: object,
  signal?: AbortSignal,
): Promise<Response> {
  const response = await fetch(`/api/${path}`, {
    method: body ? "POST" : "GET",
    headers: {
      ...(token ? { "X-Ngn-Token": token } : {}),
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
    credentials: "same-origin",
    signal,
  });
  if (!response.ok) {
    const error = (await response.json().catch(() => ({}))) as {
      detail?: string;
    };
    throw new Error(
      error.detail || `Local request failed (${response.status}).`,
    );
  }
  return response;
}

function ApprovalDialog({
  approval,
  busy,
  error,
  decide,
  cancel,
}: {
  approval: Approval;
  busy: boolean;
  error: string;
  decide: (decision: "allow" | "deny") => void;
  cancel: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const deny = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element?.showModal();
    deny.current?.focus();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      aria-labelledby="approval-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) decide("deny");
      }}
    >
      <div className="eyebrow">Human approval / one call only</div>
      <h2 id="approval-title">Allow {approval.tool}?</h2>
      <p>{approval.description}</p>
      <dl>
        <dt>Call</dt>
        <dd>{approval.call_id}</dd>
      </dl>
      <pre className="approval-preview">
        {approval.preview || preview(approval.arguments)}
      </pre>
      {approval.preview && (
        <details>
          <summary>Tool arguments</summary>
          <pre>{preview(approval.arguments)}</pre>
        </details>
      )}
      <p className="muted">
        Review the exact operation. Local tools are not sandboxed. Deny is the
        default; Escape denies this call.
      </p>
      {error && (
        <p role="alert" className="error-text">
          {error}
        </p>
      )}
      <div className="dialog-actions">
        <button className="cancel" onClick={cancel}>
          Stop run
        </button>
        <button ref={deny} disabled={busy} onClick={() => decide("deny")}>
          Deny
        </button>
        <button
          className="primary"
          disabled={busy}
          onClick={() => decide("allow")}
        >
          Allow Once
        </button>
      </div>
    </dialog>
  );
}

function App() {
  const [config, setConfig] = useState<Bootstrap>();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState("");
  const [externalRun, setExternalRun] = useState("");
  const [error, setError] = useState("");
  const [status, setStatus] = useState("Connecting to local harness");
  const [approval, setApproval] = useState<Approval>();
  const [deciding, setDeciding] = useState(false);
  const [approvalError, setApprovalError] = useState("");
  const [navOpen, setNavOpen] = useState(false);
  const stream = useRef<AbortController | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const composer = useRef<HTMLTextAreaElement>(null);

  function applySnapshot(snapshot: Snapshot) {
    setSessions(snapshot.sessions);
    setSessionId(snapshot.session_id);
    let history: Entry[] = [];
    for (const message of snapshot.history) {
      if (message.role === "tool") {
        history = appendEvent(history, {
          event: "tool_result",
          id: message.tool_call_id,
          name: message.name,
          result: message.content,
        });
        continue;
      }
      if (message.content)
        history.push({
          kind: message.role === "user" ? "user" : "assistant",
          text: message.content,
          title: message.name,
        });
      for (const call of message.tool_calls)
        history.push({
          kind: "tool",
          title: call.name,
          callId: call.id,
          text: preview(call.arguments),
        });
    }
    setEntries(history);
    stickToBottom.current = true;
  }

  async function connect() {
    setBusy(true);
    setError("");
    setSessionId("");
    try {
      const data = (await (await request("bootstrap")).json()) as Bootstrap;
      setConfig(data);
      setExternalRun(data.active_run_id);
      if (data.active_run_id) {
        setStatus("A run is active in another connection");
        return;
      }
      applySnapshot(
        (await (await request("sessions", data.token)).json()) as Snapshot,
      );
      setStatus("Ready");
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Unable to connect to ngn.",
      );
      setStatus("Disconnected");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void connect();
    return () => stream.current?.abort();
  }, []);

  useEffect(() => {
    if (stickToBottom.current && feed.current)
      feed.current.scrollTop = feed.current.scrollHeight;
  }, [entries, approval]);

  async function changeSession(id = "") {
    if (!config || busy || runId) return;
    setBusy(true);
    setError("");
    try {
      const response = await request(
        id ? "sessions/resume" : "sessions/new",
        config.token,
        id ? { session_id: id } : {},
      );
      applySnapshot((await response.json()) as Snapshot);
      setStatus("Ready");
      setNavOpen(false);
      composer.current?.focus();
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Session operation failed.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function submit(value = prompt) {
    if (!config || !sessionId || busy || externalRun || !value.trim()) return;
    const controller = new AbortController();
    stream.current = controller;
    setBusy(true);
    setError("");
    setStatus("Starting run");
    let started = false;
    let finished = false;
    let compacting = false;
    try {
      const response = await request(
        "run",
        config.token,
        { session_id: sessionId, prompt: value },
        controller.signal,
      );
      if (!response.body)
        throw new Error("Streaming is unavailable in this browser.");
      setPrompt("");
      setEntries((current) => [
        ...appendEvent(current, { event: "run_finished" }),
        { kind: "user", text: value },
      ]);
      stickToBottom.current = true;
      await readEvents(response.body, (event: WireEvent) => {
        if (event.event === "run_started") {
          started = true;
          setRunId(text(event, "run_id"));
          setStatus("Working");
        }
        if (event.event === "approval") {
          setApproval({
            run_id: text(event, "run_id"),
            approval_id: text(event, "approval_id"),
            call_id: text(event, "id"),
            tool: text(event, "tool"),
            description: text(event, "description"),
            preview: text(event, "preview"),
            arguments: event.arguments,
          });
          setApprovalError("");
          setStatus("Waiting for approval");
        }
        if (event.event === "approval_closed") {
          setApproval(undefined);
          setStatus("Working");
        }
        if (event.event === "compaction_started") compacting = true;
        if (event.event === "compaction_done") compacting = false;
        if (event.event === "run_finished") {
          finished = true;
          setApproval(undefined);
          setStatus(
            text(event, "status") === "completed"
              ? "Ready"
              : `Run ${text(event, "status")}. Completed actions were not rolled back.`,
          );
        }
        if (!compacting || !["text_chunk", "text_done"].includes(event.event))
          setEntries((current) => appendEvent(current, event));
      });
      if (!finished)
        throw new Error(
          "Stream disconnected. Partial output is kept; completed actions were not rolled back. Reconnect before another prompt.",
        );
      // Refresh navigation only: cancelled/failed partial text need not be persisted by the Harness.
      const snapshot = (await (
        await request("sessions", config.token)
      ).json()) as Snapshot;
      setSessions(snapshot.sessions);
    } catch (cause) {
      controller.abort();
      setSessionId("");
      setError(
        cause instanceof Error
          ? cause.message
          : "Connection lost. No prompt was retried.",
      );
      setStatus(
        started && !finished
          ? "Disconnected / partial output retained"
          : "Request failed",
      );
    } finally {
      stream.current = null;
      setRunId("");
      setApproval(undefined);
      setBusy(false);
      composer.current?.focus();
    }
  }

  async function cancel(id = runId || externalRun) {
    if (!config || !id) return;
    try {
      setStatus("Cancelling and waiting for tools to stop");
      await request("cancel", config.token, { run_id: id });
      if (externalRun) {
        setExternalRun("");
        await connect();
      }
    } catch (cause) {
      stream.current?.abort();
      setError(
        cause instanceof Error
          ? cause.message
          : "Cancel failed. Reconnect to check the harness.",
      );
    }
  }

  async function decide(decision: "allow" | "deny") {
    if (!config || !approval || deciding) return;
    setDeciding(true);
    setApprovalError("");
    try {
      await request("approval", config.token, {
        run_id: approval.run_id,
        approval_id: approval.approval_id,
        call_id: approval.call_id,
        decision,
      });
      setApproval(undefined);
      setStatus("Working");
    } catch (cause) {
      setApprovalError(
        cause instanceof Error
          ? cause.message
          : "Decision not confirmed. Cancel the run if disconnected.",
      );
    } finally {
      setDeciding(false);
    }
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#composer">
        Skip to prompt
      </a>
      <aside
        className={`sidebar ${navOpen ? "open" : ""}`}
        aria-label="Workspace sessions"
      >
        <div className="brand">
          ngn<span>/ local</span>
        </div>
        <div className="workspace-label">WORKSPACE</div>
        <div className="workspace" title={config?.workspace}>
          {config?.workspace || "Connecting..."}
        </div>
        <button
          className="new-session"
          disabled={busy || !!externalRun}
          onClick={() => void changeSession()}
        >
          + New session
        </button>
        <div className="workspace-label session-label">
          SESSIONS <span>{sessions.length}</span>
        </div>
        <nav aria-label="Sessions">
          {sessions.map((session) => (
            <button
              key={session.id}
              className={`session ${sessionId === session.id ? "selected" : ""}`}
              aria-current={sessionId === session.id ? "page" : undefined}
              disabled={busy || !!externalRun}
              onClick={() => void changeSession(session.id)}
              title={session.title}
            >
              <span>{session.title}</span>
              <small>{session.updated_at.slice(0, 10)}</small>
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <span className="local-dot" />
          Loopback only
          <p>
            Your workspace. Your tools.
            <br />
            Approvals stay in your hands.
          </p>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <button
            className="nav-toggle"
            aria-label="Toggle sessions"
            aria-expanded={navOpen}
            onClick={() => setNavOpen(!navOpen)}
          >
            Sessions
          </button>
          <div className="model">
            <span>{config?.agent || "ngn"}</span>
            <strong>{config?.model || "local harness"}</strong>
          </div>
          <span className={`mode-badge ${config?.demo ? "demo" : ""}`}>
            {config?.demo ? "OFFLINE DEMO" : "LOCAL CLIENT"}
          </span>
        </header>
        <div
          className="conversation"
          ref={feed}
          onScroll={() => {
            const el = feed.current;
            if (el)
              stickToBottom.current =
                el.scrollHeight - el.scrollTop - el.clientHeight < 100;
          }}
        >
          <div className="conversation-inner">
            {!entries.length && (
              <section className="welcome">
                <div className="eyebrow">ngn / coding harness</div>
                <h1>
                  A little less ceremony.
                  <br />A little more building.
                </h1>
                <p>
                  Inspect your workspace, follow the tools, and review each
                  proposed action. The same ngn Harness, now in your browser.
                </p>
                {config?.demo ? (
                  <>
                    <p className="demo-note">
                      Offline demo. Scripted responses, real local tools and
                      sessions. No provider requests, shell commands, or
                      workspace writes.
                    </p>
                    <div className="starters">
                      <button
                        disabled={busy || !!externalRun}
                        onClick={() => void submit("Show me this workspace")}
                      >
                        Inspect workspace <span>&#8599;</span>
                      </button>
                      <button
                        disabled={busy || !!externalRun}
                        onClick={() => void submit("demo approval")}
                      >
                        Try an approval <span>&#8599;</span>
                      </button>
                    </div>
                  </>
                ) : (
                  <p className="demo-note">
                    Live provider requests may incur costs. Shell and edits
                    require approval. This is a trusted local tool, not a
                    sandbox.
                  </p>
                )}
              </section>
            )}
            {entries.map((entry, index) => (
              <article key={index} className={`entry ${entry.kind}`}>
                {entry.kind === "tool" ? (
                  <details>
                    <summary>
                      <span className="tool-mark">&#9656;</span>
                      {entry.title || "Tool activity"}
                      <span className="detail-hint">details</span>
                    </summary>
                    <pre>{entry.text}</pre>
                  </details>
                ) : (
                  <>
                    <div className="entry-label">
                      {entry.kind === "assistant"
                        ? "ngn"
                        : entry.kind === "user"
                          ? "you"
                          : entry.kind}
                    </div>
                    <div className="entry-content">
                      {entry.text}
                      {entry.streaming && busy && <span className="cursor" />}
                    </div>
                  </>
                )}
              </article>
            ))}
          </div>
        </div>
        <footer className="composer-area">
          {(error || externalRun) && (
            <div role="alert" className="error-banner">
              <span>
                {error ||
                  "Another connection owns the active run. Finish or cancel it before changing sessions."}
              </span>
              {externalRun && (
                <button onClick={() => void cancel()}>Cancel active run</button>
              )}
              {!busy && (
                <button onClick={() => void connect()}>Reconnect</button>
              )}
            </div>
          )}
          <div className="run-status" role="status">
            <span className={busy ? "working-dot" : "local-dot"} />
            {status}
          </div>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            <label htmlFor="composer" className="sr-only">
              Message ngn
            </label>
            <textarea
              ref={composer}
              id="composer"
              placeholder={
                config?.demo
                  ? "Ask about the workspace, or try 'demo approval'..."
                  : "What should we work on?"
              }
              value={prompt}
              maxLength={32000}
              rows={2}
              disabled={!config || !!externalRun}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  void submit();
                }
              }}
            />
            <div className="composer-bottom">
              <span>
                Enter to send <b>/</b> Shift+Enter for newline
              </span>
              {busy && runId ? (
                <button
                  type="button"
                  className="cancel"
                  onClick={() => void cancel()}
                >
                  Stop run
                </button>
              ) : (
                <button
                  type="submit"
                  className="primary"
                  disabled={
                    busy ||
                    !config ||
                    !sessionId ||
                    !!externalRun ||
                    !prompt.trim()
                  }
                >
                  Send <span aria-hidden="true">&#8593;</span>
                </button>
              )}
            </div>
          </form>
          <div className="bottom-note">
            {config?.provider || "ngn"} <span>/</span>{" "}
            {config?.demo
              ? "No paid requests"
              : "Approvals are per call, never automatic"}{" "}
            <span>/</span> {sessionId.slice(-8)}
          </div>
        </footer>
      </main>
      {approval && (
        <ApprovalDialog
          key={approval.approval_id}
          approval={approval}
          busy={deciding}
          error={approvalError}
          decide={(decision) => void decide(decision)}
          cancel={() => void cancel()}
        />
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
