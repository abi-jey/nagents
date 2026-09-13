import { useCallback, useRef, useState } from "react";
import { Icon } from "../components/Icon.js";
import { ApprovalDialog } from "../features/approvals/ApprovalDialog";
import { Composer } from "../features/chat/Composer";
import { Conversation } from "../features/chat/Conversation";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { DeleteSessionDialog } from "../features/sessions/DeleteSessionDialog";
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
  const closeNavigation = useCallback(() => setNavOpen(false), []);
  const composer = useRef<HTMLTextAreaElement>(null);
  const selectedSession = sessions.sessions.find((session) => session.id === sessionId);
  const canSubmit =
    !!config && !!sessionId && !client.operating && !dictation.unfinished && !client.channels.open && !client.settings.open && !client.deletion.target;
  const runStatus = chat.status + (chat.pendingWakeups
    ? `; ${chat.pendingWakeups} scheduled wake-up${chat.pendingWakeups === 1 ? "" : "s"}` : "");

  async function select(id?: string) {
    if (await client.select(id)) {
      setNavOpen(false);
      requestAnimationFrame(() => composer.current?.focus());
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
            <Icon name="menu" />
          </button>
          <div className="model">
            <h1 title={selectedSession?.title || "New session"}>{selectedSession?.title || "New session"}</h1>
            <span
              title={
                config ? `${config.agent}: ${config.model}` : "Local harness"
              }
            >
              {config ? `${config.agent}: ${config.model}` : "Local harness"}
            </span>
          </div>
        </header>
        <SessionSidebar
          workspace={config?.workspace || ""}
          sessions={sessions.sessions}
          selected={sessionId}
          disabled={client.operating || dictation.unfinished || client.channels.open || client.settings.open || !!client.deletion.target}
          newDisabled={busy}
          open={navOpen}
          select={(id) => void select(id)}
          remove={(session) => client.deletionController.show(session)}
          canDelete={(id) => !client.deletion.target && client.canDeleteSession(id)}
          settings={client.settings.show}
          settingsDisabled={!config || busy || !!externalRun || !!chat.approval.pending || dictation.unfinished || client.channels.open || !!client.deletion.target}
          channels={client.channels.show}
          channelsDisabled={!config || client.operating || !!chat.approval.pending || dictation.unfinished || client.settings.open || !!client.deletion.target}
          demo={!!config?.demo}
          close={closeNavigation}
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
      {client.deletion.target && <DeleteSessionDialog state={client.deletion} controller={client.deletionController} currentSessionId={sessionId} />}
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
