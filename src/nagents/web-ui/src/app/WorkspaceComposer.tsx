import type { RefObject } from "react";
import { Composer } from "../features/chat/Composer";
import { UploadPreviews } from "../features/chat/UploadPreviews";
import { DictationControls, DictationReview } from "../features/dictation/DictationControls";
import type { Client } from "./useClient";

export function WorkspaceComposer({ client, composer, submit, select }: {
  client: Client;
  composer: RefObject<HTMLTextAreaElement | null>;
  submit: () => void;
  select: (id?: string) => Promise<void>;
}) {
  const { chat, sessions, dictation, busy } = client;
  const { config, sessionId, externalRun } = sessions;
  const activeTitle = sessions.sessions.find((session) => session.id === sessions.activeSessionId)?.title || "another session";
  const runStatus = chat.status + (chat.pendingWakeups
    ? `; ${chat.pendingWakeups} scheduled wake-up${chat.pendingWakeups === 1 ? "" : "s"}` : "");

  return <footer className="composer-area">
    {client.error && <div className="error-banner" role="alert">
      <span>{client.error}</span>
      <div className="feedback-actions">
        <button disabled={client.operating || dictation.unfinished} onClick={() => void client.connect()}>Reconnect</button>
        <button aria-label="Dismiss error" onClick={client.dismissError}>Dismiss</button>
      </div>
    </div>}
    {externalRun && <div className="activity-banner">
      <span role="status">Working in <strong>{activeTitle}</strong>. Messages sent here will wait their turn.</span>
      <div className="feedback-actions">
        <button disabled={!client.available.navigate} onClick={() => void select(sessions.activeSessionId)}>View active session</button>
        <button disabled={chat.stopping} onClick={() => void client.cancel()}>{chat.stopping ? "Stopping…" : "Stop active run"}</button>
      </div>
    </div>}
    {chat.activityError && <p className="activity-warning" role="status">{chat.activityError}</p>}
    <Composer inputRef={composer} prompt={chat.prompt} setPrompt={chat.setPrompt} demo={!!config?.demo}
      disabled={!config || !sessionId} attachmentsDisabled={!config || client.operating || dictation.unfinished}
      submitting={chat.submitting} stopping={chat.stopping}
      attachmentTypes={client.uploadState.types} hasAttachments={!!client.uploadState.items.length}
      addAttachments={(files) => void client.uploads.add(files)}
      attachments={<UploadPreviews state={client.uploadState} disabled={client.operating} remove={(id) => client.uploads.remove(id)} />}
      canSubmit={client.available.submit} running={busy && !!chat.runId} submit={submit} cancel={() => void client.cancel()}
      status={<span className={`run-status${chat.status === "Ready" && !chat.pendingWakeups ? " sr-only" : ""}`} role="status" title={runStatus}>{runStatus}</span>}
      review={<DictationReview state={dictation.state} edit={dictation.controller.edit}
        insert={() => { if (client.insertDictation()) composer.current?.focus({ preventScroll: true }); }}
        cancel={() => dictation.controller.cancel()} />}
      dictation={<DictationControls state={dictation.state} config={config?.dictation} unsupported={dictation.unsupported}
        disabled={!client.available.create} start={client.startDictation} stop={dictation.controller.stop}
        cancel={() => dictation.controller.cancel()} transcribe={() => void dictation.controller.transcribe()} />}
    />
  </footer>;
}
