import { useMemo, useState, type RefObject } from "react";
import { Icon } from "../components/Icon";
import { Composer } from "../features/chat/Composer";
import { UploadPreviews } from "../features/chat/UploadPreviews";
import { DraftRecoveryNotice } from "../features/chat/DraftRecoveryNotice.js";
import { LiveDialog } from "../features/live/LiveDialog.js";
import { isProfileTarget } from "../features/team/useTeam.js";
import type { LiveComposerTargets } from "../features/live/LiveComposer.js";
import "../features/live/live.css";
import "../features/live/composer.css";
import "../features/live/voiceControls.css";
import "../features/live/sharedComposer.css";
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
  const { team } = client;
  const [voiceTargets, setVoiceTargets] = useState<LiveComposerTargets>({ presence: null, caption: null, controls: null, details: null });
  const voiceRefs = useMemo(() => {
    const bind = (slot: keyof LiveComposerTargets) => (element: HTMLDivElement | null) => {
      setVoiceTargets(current => current[slot] === element ? current : { ...current, [slot]: element });
    };
    return { presence: bind("presence"), caption: bind("caption"), controls: bind("controls"), details: bind("details") };
  }, []);
  const child = !!team.selected;
  const recipient = team.task?.name || team.profile?.name || (isProfileTarget(team.selected) ? team.selected.slice("profile:".length) : "agent");
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
    {child && <div className="composer-recipient"><span>To <strong>{recipient}</strong></span><button type="button" onClick={() => team.select("")}>Back to Assistant</button></div>}
    {child && team.error && <p className="activity-warning" role="alert">{team.error}<button type="button" onClick={team.dismissError}>Dismiss</button></p>}
    {child && !team.canMessage && <p className="activity-warning">{team.task?.unavailable_reason || team.profile?.unavailable_reason || team.task?.message_unavailable_reason || "This agent is not available for messages."}</p>}
    {child && client.liveOpen && <p className="team-voice-notice">Voice remains in the main conversation.</p>}
    <div hidden={child} className="workspace-live-slot">{client.liveOpen && config && <LiveDialog key={`${config.token}:${sessionId}`} token={config.token} sessionId={sessionId}
      assistant={client.sessionAgent.identity}
      targets={voiceTargets} visible={!child}
      autoStart={client.liveAutoStart}
      openSettingsInitially={client.liveIntent === "settings"} consumeStartIntent={client.consumeLiveStart}
      close={() => { client.closeLive(); requestAnimationFrame(() => document.getElementById("live-launch")?.focus()); }}
      configureConnection={() => { client.closeLive(); client.settings.show(); }} />}</div>
    <Composer inputRef={composer} prompt={child ? team.prompt : chat.prompt} setPrompt={child ? team.setPrompt : chat.setPrompt} demo={!!config?.demo}
      voiceLayout={client.liveOpen && !child} voiceRefs={voiceRefs}
      recipient={child ? recipient : "ngn"} commandsEnabled={!child} maxLength={child ? 16000 : 32000}
      disabled={!config || !sessionId} attachmentsDisabled={!config || client.operating}
      submitting={child ? team.submitting : chat.submitting} stopping={chat.stopping}
      attachmentTypes={child ? [] : client.uploadState.types} hasAttachments={!child && !!client.uploadState.items.length}
      addAttachments={child ? undefined : (files) => void client.uploads.add(files)}
      attachments={!child && <UploadPreviews state={client.uploadState} disabled={client.operating} remove={(id) => client.uploads.remove(id)} />}
      canSubmit={child ? !!config && team.canMessage && !team.submitting : client.available.submit} running={!child && busy && !!chat.runId} submit={submit} cancel={() => void client.cancel()}
      skills={client.skills.items} skillsLoading={client.skills.loading} skillsError={client.skills.error}
      refreshSkills={() => void client.skills.refresh()}
      voiceControl={!child && !client.liveOpen && <div className="voice-entry"><button type="button" id="live-launch" className="live-launch" aria-label="Start voice" title="Talk in this chat" disabled={!client.available.live} onClick={() => client.showLive()}><Icon name="wave" size={17} /><span>Voice</span></button><button type="button" className="voice-entry-settings" aria-label="Voice settings" title="Voice settings" aria-haspopup="dialog" aria-controls="voice-settings-dialog" disabled={!client.available.live} onClick={() => client.showLive("settings")}><Icon name="settings" size={16} /></button></div>}
      status={child ? <span className="run-status" role="status">{team.submitting ? "Sending…" : !team.canMessage ? "Unavailable" : team.profile ? "Starts this agent" : "Joins the next response"}</span> : <span className={`run-status${chat.status === "Ready" && !chat.pendingWakeups ? " sr-only" : ""}`} role="status" title={runStatus}>{runStatus}</span>}
    />
  </footer>;
}
