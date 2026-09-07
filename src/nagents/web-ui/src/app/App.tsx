import { useRef, useState } from "react";
import { ApprovalDialog } from "../features/approvals/ApprovalDialog";
import { Composer } from "../features/chat/Composer";
import { Conversation } from "../features/chat/Conversation";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { useClient } from "./useClient";

export function App() {
  const client = useClient();
  const { chat, sessions, busy, error } = client;
  const { config, sessionId, externalRun } = sessions;
  const [navOpen, setNavOpen] = useState(false);
  const composer = useRef<HTMLTextAreaElement>(null);
  const canSubmit = !!config && !!sessionId && !busy && !externalRun;

  async function select(id?: string) {
    if (await client.select(id)) {
      setNavOpen(false);
      composer.current?.focus();
    }
  }
  async function submit(value?: string) {
    await client.submit(value);
    composer.current?.focus();
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#composer">
        Skip to prompt
      </a>
      <SessionSidebar
        workspace={config?.workspace || ""}
        sessions={sessions.sessions}
        selected={sessionId}
        disabled={busy || !!externalRun}
        open={navOpen}
        select={(id) => void select(id)}
      />
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
        <Conversation
          entries={chat.entries}
          sessionId={sessionId}
          busy={busy}
          demo={!!config?.demo}
          canSubmit={canSubmit}
          submit={(value) => void submit(value)}
        />
        <footer className="composer-area">
          {(error || externalRun) && (
            <div role="alert" className="error-banner">
              <span>
                {error ||
                  "Another connection owns the active run. Finish or cancel it before changing sessions."}
              </span>
              {externalRun && (
                <button onClick={() => void client.cancel()}>
                  Cancel active run
                </button>
              )}
              {!busy && (
                <button onClick={() => void client.connect()}>Reconnect</button>
              )}
            </div>
          )}
          <div className="run-status" role="status">
            <span className={busy ? "working-dot" : "local-dot"} />
            {chat.status}
          </div>
          <Composer
            inputRef={composer}
            prompt={chat.prompt}
            setPrompt={chat.setPrompt}
            demo={!!config?.demo}
            disabled={!config || !!externalRun}
            canSubmit={canSubmit}
            running={busy && !!chat.runId}
            submit={() => void submit()}
            cancel={() => void client.cancel()}
          />
          <div className="bottom-note">
            {config?.provider || "ngn"} <span>/</span>{" "}
            {config?.demo
              ? "No paid requests"
              : "Approvals are per call, never automatic"}{" "}
            <span>/</span> {sessionId.slice(-8)}
          </div>
        </footer>
      </main>
      {chat.approval.pending && (
        <ApprovalDialog
          key={chat.approval.pending.approval_id}
          approval={chat.approval.pending}
          busy={chat.approval.deciding}
          error={chat.approval.error}
          decide={(decision) => void chat.approval.decide(decision)}
          cancel={() => void client.cancel()}
        />
      )}
    </div>
  );
}
