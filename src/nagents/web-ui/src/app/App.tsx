import { useRef, useState } from "react";
import { ApprovalDialog } from "../features/approvals/ApprovalDialog";
import { Composer } from "../features/chat/Composer";
import { Conversation } from "../features/chat/Conversation";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { ChannelsDialog } from "../features/channels/ChannelsDialog";
import { DictationControls, DictationReview } from "../features/dictation/DictationControls";
import "../features/dictation/dictation.css";
import "../features/settings/settings.css";
import "../features/channels/channels.css";
import { useClient } from "./useClient";

export function App() {
  const client = useClient();
  const { chat, sessions, busy, error, dictation } = client;
  const { config, sessionId, externalRun } = sessions;
  const [navOpen, setNavOpen] = useState(false);
  const composer = useRef<HTMLTextAreaElement>(null);
  const canSubmit =
    !!config && !!sessionId && !client.operating && !dictation.unfinished && !client.channels.open && !client.settings.open;
  const runStatus = chat.status + (chat.pendingWakeups
    ? `; ${chat.pendingWakeups} scheduled wake-up${chat.pendingWakeups === 1 ? "" : "s"}` : "");

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
            title="Sessions"
            onClick={() => setNavOpen(!navOpen)}
          >
            <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M3 5h14M3 10h14M3 15h14" /></svg>
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
            className="channels-trigger" aria-label="Channels" title="Channels" aria-haspopup="dialog"
            disabled={!config || client.operating || !!chat.approval.pending || dictation.unfinished || client.settings.open}
            onClick={client.channels.show}
          >
            <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><rect x="2" y="6" width="5" height="8" rx="1" /><rect x="13" y="2" width="5" height="6" rx="1" /><rect x="13" y="12" width="5" height="6" rx="1" /><path d="M7 10h3V5h3M10 10v5h3" /></svg>
            <span className="desktop-label">Channels</span>
          </button>
          <button
            className="settings-trigger"
            aria-label="Settings"
            title="Settings"
            disabled={
              !config || busy || !!externalRun || !!chat.approval.pending || dictation.unfinished
            }
            onClick={client.settings.show}
            aria-haspopup="dialog"
          >
            <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M3 5h14M3 10h14M3 15h14" /><path d="M7 3v4M13 8v4M8 13v4" /></svg>
            <span className="desktop-label">Settings</span>
          </button>
          <span className={`mode-badge ${config?.demo ? "demo" : ""}`} title={config?.demo ? "Offline demo" : "Live provider"}>
            {config?.demo ? "Demo" : "Live"}
          </span>
        </header>
        <SessionSidebar
          workspace={config?.workspace || ""}
          sessions={sessions.sessions}
          selected={sessionId}
          disabled={client.operating || dictation.unfinished || client.channels.open || client.settings.open}
          newDisabled={busy}
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
                    ? "The harness is working in another session. New messages are queued."
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
          {chat.activityError && (
            <p className="activity-warning" role="status">
              {chat.activityError}
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
            status={
              <span className={`run-status${chat.status === "Ready" && !chat.pendingWakeups ? " sr-only" : ""}`} role="status" title={runStatus}>
                {runStatus}
              </span>
            }
            review={
              <DictationReview
                state={dictation.state}
                edit={dictation.controller.edit}
                insert={() => { if (client.insertDictation()) composer.current?.focus({ preventScroll: true }); }}
                cancel={() => dictation.controller.cancel()}
              />
            }
            dictation={
              <DictationControls
                state={dictation.state}
                config={config?.dictation}
                unsupported={dictation.unsupported}
                disabled={!config || !sessionId || busy || !!externalRun || sessions.activityOnly || client.settings.open || client.channels.open}
                start={client.startDictation}
                stop={dictation.controller.stop}
                cancel={() => dictation.controller.cancel()}
                transcribe={() => void dictation.controller.transcribe()}
                settings={client.settings.show}
              />
            }
          />
        </footer>
      </main>
      {client.settings.open && <SettingsDialog settings={client.settings} />}
      {client.channels.open && <ChannelsDialog channels={client.channels} sessions={sessions.sessions} selected={sessionId} />}
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
