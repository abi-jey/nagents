import type { RefObject } from "react";
import { Icon } from "../components/Icon";
import { Composer } from "../features/chat/Composer";
import { UploadPreviews } from "../features/chat/UploadPreviews";
import { DraftRecoveryNotice } from "../features/chat/DraftRecoveryNotice.js";
import { LiveDialog } from "../features/live/LiveDialog.js";
import "../features/live/live.css";
import "../features/live/composer.css";
import "../features/live/sphere.css";
import "../features/live/settingsDialog.css";
import "../features/live/inspection.css";
import type { Client } from "./useClient";

export function WorkspaceComposer({ client, composer, submit, select }: {
  client: Client;
  composer: RefObject<HTMLTextAreaElement | null>;
  submit: () => void;
  select: (id?: string) => Promise<void>;
}) {
  const { chat, sessions, busy } = client;
  const { config, sessionId, externalRun } = sessions;
  const activeTitle = sessions.sessions.find((session) => session.id === sessions.activeSessionId)?.title || "another session";
  const runStatus = chat.status + (chat.pendingWakeups
    ? `; ${chat.pendingWakeups} scheduled wake-up${chat.pendingWakeups === 1 ? "" : "s"}` : "");

  return <footer className="composer-area">
    <div id="approval-waiting-slot" style={{ display: "contents" }} />
    {client.error && <div className="error-banner" role="alert">
      <span>{client.error}</span>
      <div className="feedback-actions">
        <button disabled={client.operating} onClick={() => void client.connect()}>Reconnect</button>
        <button aria-label="Dismiss error" onClick={client.dismissError}>Dismiss</button>
      </div>
    </div>}
    {externalRun && <div className="activity-banner">
      <span role="status">Work continues in <strong>{activeTitle}</strong>. You can work in this chat too.</span>
      <div className="feedback-actions">
        <button disabled={!client.available.navigate} onClick={() => void select(sessions.activeSessionId)}>View active session</button>
      </div>
    </div>}
    {chat.activityError && <p className="activity-warning" role="status">{chat.activityError}</p>}
    <DraftRecoveryNotice drafts={client.orphanedDrafts} disabled={!client.available.create} recover={(id) => {
      void client.recoverDraft(id).then((recovered) => { if (recovered) requestAnimationFrame(() => composer.current?.focus()); });
    }} />
    {client.liveOpen && config && <LiveDialog key={`${config.token}:${sessionId}`} token={config.token} sessionId={sessionId}
      autoStart={client.liveAutoStart}
      openSettingsInitially={client.liveIntent === "settings"} consumeStartIntent={client.consumeLiveStart}
      close={() => { client.closeLive(); requestAnimationFrame(() => document.getElementById("live-launch")?.focus()); }}
      configureConnection={() => { client.closeLive(); client.settings.show(); }} />}
    <Composer inputRef={composer} prompt={chat.prompt} setPrompt={chat.setPrompt} demo={!!config?.demo}
      disabled={!config || !sessionId} attachmentsDisabled={!config || client.operating}
      submitting={chat.submitting} stopping={chat.stopping}
      attachmentTypes={client.uploadState.types} hasAttachments={!!client.uploadState.items.length}
      addAttachments={(files) => void client.uploads.add(files)}
      attachments={<UploadPreviews state={client.uploadState} disabled={client.operating} remove={(id) => client.uploads.remove(id)} />}
      canSubmit={client.available.submit} running={busy && !!chat.runId} submit={submit} cancel={() => void client.cancel()}
      voiceControl={!client.liveOpen && <div className="voice-entry"><button type="button" id="live-launch" className="live-launch" aria-label="Start voice" title="Talk in this chat" disabled={!client.available.live} onClick={() => client.showLive()}><Icon name="wave" size={17} /><span>Voice</span></button><button type="button" className="voice-entry-settings" aria-label="Voice settings" title="Voice settings" aria-haspopup="dialog" aria-controls="voice-settings-dialog" disabled={!client.available.live} onClick={() => client.showLive("settings")}><Icon name="settings" size={16} /></button></div>}
      status={<span className={`run-status${chat.status === "Ready" && !chat.pendingWakeups ? " sr-only" : ""}`} role="status" title={runStatus}>{runStatus}</span>}
    />
  </footer>;
}
