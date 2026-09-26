import { useCallback, useRef, useState } from "react";
import { Icon } from "../components/Icon.js";
import { ApprovalDialog } from "../features/approvals/ApprovalDialog";
import { Composer } from "../features/chat/Composer";
import { Conversation } from "../features/chat/Conversation";
import { UploadPreviews } from "../features/chat/UploadPreviews";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { DeleteSessionDialog } from "../features/sessions/DeleteSessionDialog";
import { TrashDialog, TrashNotice } from "../features/sessions/TrashDialog";
import "../features/sessions/trash.css";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { ChannelsDialog } from "../features/channels/ChannelsDialog";
import { DictationControls, DictationReview } from "../features/dictation/DictationControls";
import "../features/dictation/dictation.css";
import "../features/settings/settings.css";
import "../features/channels/channels.css";
import { useClient } from "./useClient";
import { ContextIndicator } from "../features/context/ContextIndicator";
import { restoreDeletionFocus } from "../api/deletion";
import { Designer } from "../features/designer/Designer";
import { ToolsDialog } from "../features/tools/ToolsDialog";

export function App() {
  const client = useClient();
  const { chat, sessions, busy, error, dictation } = client;
  const { config, sessionId, externalRun } = sessions;
  const [navOpen, setNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [designerOpen, setDesignerOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const closeNavigation = useCallback(() => setNavOpen(false), []);
  const composer = useRef<HTMLTextAreaElement>(null);
  const selectedSession = sessions.sessions.find((session) => session.id === sessionId);
  const canSubmit =
    !!config && !!sessionId && !client.operating && !client.uploadState.items.some((item) => item.status !== "ready") && !dictation.unfinished && !client.channels.open && !client.settings.open && !client.deletion.target && !client.trash.open && !toolsOpen;
  const trashBlocked = busy || (dictation.unfinished && dictation.state.phase !== "review");
  function openRestored(id: string) { client.trashController.close(); void select(id); }
  async function moveToTrash(session: typeof sessions.sessions[number]) {
    const previous = document.activeElement;
    if (await client.softDelete(session)) requestAnimationFrame(() => {
      if (document.activeElement !== document.body && document.activeElement !== previous) return;
      restoreDeletionFocus(previous instanceof HTMLElement ? previous : null, composer.current,
        document.querySelector<HTMLElement>("#session-navigation.open[role='dialog'][aria-modal='true']"));
    });
  }
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
      <main inert={designerOpen} className={sidebarCollapsed ? "sidebar-collapsed" : undefined}>
        <header className="topbar">
          <button title="Experimental agent designer" disabled={!config || busy || client.operating || dictation.unfinished} onClick={() => setDesignerOpen(true)}>Agent Designer</button>
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
          <ContextIndicator
            stats={client.context.stats}
            error={client.context.error}
            open={client.context.open}
            setOpen={client.context.setOpen}
            live={client.context.live}
            loading={client.context.loading}
            refresh={client.context.refresh}
          />
        </header>
        <SessionSidebar
          workspace={config?.workspace || ""}
          sessions={sessions.sessions}
          selected={sessionId}
          disabled={client.operating || dictation.unfinished || client.channels.open || client.settings.open || !!client.deletion.target || client.trash.open}
          newDisabled={busy}
          open={navOpen}
          select={(id) => void select(id)}
          remove={(session) => void moveToTrash(session)}
          permanent={(session) => client.deletionController.show(session)}
          canDelete={(id) => !client.deletion.target && client.canDeleteSession(id)}
          trash={client.trashController.show}
          trashDisabled={!config || client.operating || (dictation.unfinished && dictation.state.phase !== "review") || client.channels.open || client.settings.open || !!client.deletion.target}
          notice={!client.trash.open && <TrashNotice state={client.trash} controller={client.trashController} openSession={openRestored} blocked={trashBlocked} openDisabled={dictation.unfinished} />}
          settings={client.settings.show}
          globalSettings={client.settings.showGlobal}
          tools={() => setToolsOpen(true)}
          toolsDisabled={!config || client.operating || dictation.unfinished}
          settingsDisabled={!config || busy || !!externalRun || !!chat.approval.pending || dictation.unfinished || client.channels.open || !!client.deletion.target || client.trash.open}
          channels={client.channels.show}
          channelsDisabled={!config || client.operating || !!chat.approval.pending || dictation.unfinished || client.settings.open || !!client.deletion.target || client.trash.open}
          demo={!!config?.demo}
          close={closeNavigation}
        />
        <button type="button" className="sidebar-divider-toggle"
          aria-label={sidebarCollapsed ? "Expand sessions sidebar" : "Collapse sessions sidebar"}
          title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-controls="session-navigation" aria-expanded={!sidebarCollapsed}
          onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}>
          <Icon name="chevron" size={14} />
        </button>
        <Conversation
          token={config?.token || ""}
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
            disabled={!config || client.operating}
            attachmentTypes={client.uploadState.types}
            hasAttachments={!!client.uploadState.items.length}
            addAttachments={(files) => void client.uploads.add(files)}
            attachments={<UploadPreviews state={client.uploadState} disabled={client.operating} remove={(id) => client.uploads.remove(id)} />}
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
                disabled={!config || !sessionId || busy || !!externalRun || sessions.activityOnly || client.settings.open || client.channels.open || client.trash.open}
                start={client.startDictation}
                stop={dictation.controller.stop}
                cancel={() => dictation.controller.cancel()}
                transcribe={() => void dictation.controller.transcribe()}
              />
            }
          />
        </footer>
      </main>
      {designerOpen && config && <Designer token={config.token} configureChannels={client.channels.show} close={() => { setDesignerOpen(false); void client.connect(); }} />}
      {client.settings.open && <SettingsDialog settings={client.settings} />}
      {toolsOpen && config && <ToolsDialog token={config.token} blocked={busy || client.operating || dictation.unfinished} close={() => { setToolsOpen(false); void client.connect(); }} />}
      {client.trash.open && <TrashDialog state={client.trash} controller={client.trashController} permanent={client.deletionController.showTrash.bind(client.deletionController)} openSession={openRestored} blocked={trashBlocked} openDisabled={dictation.unfinished} />}
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
