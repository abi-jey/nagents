import { useRef, useState } from "react";
import { ApprovalDialog } from "../features/approvals/ApprovalDialog";
import { Composer } from "../features/chat/Composer";
import { Conversation } from "../features/chat/Conversation";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { SettingsDialog } from "../features/settings/SettingsDialog";
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
    composer.current?.focus();
    await client.submit(value);
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#composer">
        Skip to prompt
      </a>
      <main>
        <header className="topbar">
          <button
            className="nav-toggle"
            aria-label="Toggle sessions"
            aria-expanded={navOpen}
            aria-controls="session-navigation"
            onClick={() => setNavOpen(!navOpen)}
          >
            Sessions
          </button>
          <div className="model">
            <h1>ngn</h1>
            <span
              title={
                config ? `${config.agent}: ${config.model}` : "Local harness"
              }
            >
              {config ? `${config.agent}: ${config.model}` : "Local harness"}
            </span>
          </div>
          <button
            className="settings-trigger"
            disabled={!config || busy || !!externalRun || !!chat.approval.pending}
            onClick={client.settings.show}
            aria-haspopup="dialog"
          >
            Settings
          </button>
          <span className={`mode-badge ${config?.demo ? "demo" : ""}`}>
            {config?.demo ? "Offline demo" : "Live provider"}
          </span>
        </header>
        <SessionSidebar
          workspace={config?.workspace || ""}
          sessions={sessions.sessions}
          selected={sessionId}
          disabled={busy || !!externalRun}
          open={navOpen}
          select={(id) => void select(id)}
        />
        <Conversation
          entries={chat.entries}
          sessionId={sessionId}
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
            {config?.demo
              ? "No paid requests. Sessions are saved locally."
              : "Approvals apply to one call. Completed actions are not rolled back."}
          </div>
        </footer>
      </main>
      {client.settings.open && <SettingsDialog settings={client.settings} />}
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
