import { useRef, useState } from "react";
import { ApprovalDialog } from "../features/approvals/ApprovalDialog";
import { Composer } from "../features/chat/Composer";
import { Conversation } from "../features/chat/Conversation";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { DictationControls } from "../features/dictation/DictationControls";
import "../features/dictation/dictation.css";
import { useClient } from "./useClient";

export function App() {
  const client = useClient();
  const { chat, sessions, busy, error, dictation } = client;
  const { config, sessionId, externalRun } = sessions;
  const [navOpen, setNavOpen] = useState(false);
  const composer = useRef<HTMLTextAreaElement>(null);
  const canSubmit =
    !!config && !!sessionId && !busy && !externalRun && !sessions.activityOnly && !dictation.unfinished;

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
            disabled={
              !config || busy || !!externalRun || !!chat.approval.pending || dictation.unfinished
            }
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
          disabled={busy || !!externalRun || dictation.unfinished}
          open={navOpen}
          select={(id) => void select(id)}
        />
        <Conversation
          key={`${sessionId}:${chat.transcriptVersion}`}
          entries={chat.entries}
          sessionId={sessionId}
          demo={!!config?.demo}
          canSubmit={canSubmit}
          submit={(value) => void submit(value)}
        />
        <footer className="composer-area">
          {(error || externalRun || sessions.activityOnly) && (
            <div role="alert" className="error-banner">
              <span>
                {error ||
                  (externalRun
                    ? "Another connection owns the active run. Finish or cancel it before changing sessions."
                    : "Connected during a background run. Earlier conversation is not loaded; reconnect when idle to load it.")}
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
            {!!chat.pendingWakeups &&
              `; ${chat.pendingWakeups} scheduled wake-up${chat.pendingWakeups === 1 ? "" : "s"}`}
          </div>
          {chat.activityError && (
            <p className="activity-warning" role="status">
              Background activity unavailable: {chat.activityError} Read-only
              polling will retry.
            </p>
          )}
          <Composer
            inputRef={composer}
            prompt={chat.prompt}
            setPrompt={chat.setPrompt}
            demo={!!config?.demo}
            disabled={!config}
            canSubmit={canSubmit}
            running={busy && !!chat.runId}
            submit={() => void submit()}
            cancel={() => void client.cancel()}
            dictation={
              <DictationControls
                state={dictation.state}
                config={config?.dictation}
                unsupported={dictation.unsupported}
                disabled={!config || !sessionId || busy || !!externalRun || sessions.activityOnly || client.settings.open}
                start={client.startDictation}
                stop={dictation.controller.stop}
                cancel={() => dictation.controller.cancel()}
                transcribe={() => void dictation.controller.transcribe()}
                edit={dictation.controller.edit}
                insert={() => { if (client.insertDictation()) composer.current?.focus({ preventScroll: true }); }}
                settings={client.settings.show}
              />
            }
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
