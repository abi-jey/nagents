import { useState, type RefObject } from "react";
import { Icon } from "../components/Icon.js";
import { SessionSidebar } from "../features/sessions/SessionSidebar.js";
import { TrashNotice } from "../features/sessions/TrashDialog.js";
import { restoreDeletionFocus } from "../api/deletion.js";
import type { Client } from "./useClient";
import { useChatFolders } from "../features/sessions/useChatFolders.js";
import { SessionTitleDialog } from "../features/sessions/SessionTitleDialog.js";
import type { Session } from "../types.js";

export function WorkspaceSidebar({ client, composer, open, close, select, collapsed, toggleCollapsed }: {
  client: Client;
  composer: RefObject<HTMLTextAreaElement | null>;
  open: boolean;
  close: () => void;
  select: (id?: string) => Promise<void>;
  collapsed: boolean;
  toggleCollapsed: () => void;
}) {
  const { sessions, available } = client;
  const [renaming, setRenaming] = useState<Session>();
  const [forkFailed, setForkFailed] = useState(false);
  const folders = useChatFolders(sessions.config?.token || "", sessions.sessions.map(session => session.id));
  async function fork(session: Session) {
    client.dismissError(); setForkFailed(false);
    if (await client.forkSession(session.id)) {
      close(); requestAnimationFrame(() => composer.current?.focus({ preventScroll: true }));
    } else setForkFailed(true);
  }
  async function moveToTrash(session: typeof sessions.sessions[number]) {
    const previous = document.activeElement;
    if (await client.softDelete(session)) requestAnimationFrame(() => {
      if (document.activeElement !== document.body && document.activeElement !== previous) return;
      restoreDeletionFocus(previous instanceof HTMLElement ? previous : null, composer.current,
        document.querySelector<HTMLElement>("#session-navigation.open[role='dialog'][aria-modal='true']"));
    });
  }
  return <>
    <SessionSidebar
      workspace={sessions.config?.workspace || ""} sessions={sessions.sessions} selected={sessions.sessionId}
      folders={folders}
      disabled={!available.navigate} newDisabled={!available.create} open={open} close={close}
      select={(id) => { setForkFailed(false); void select(id); }} remove={(session) => void moveToTrash(session)}
      permanent={(session) => client.deletionController.show(session)}
      rename={(session) => { client.dismissError(); setForkFailed(false); setRenaming(session); }}
      fork={(session) => void fork(session)} canFork={client.canForkSession}
      canDelete={(id) => !client.deletion.target && client.canDeleteSession(id)}
      trash={client.trashController.show} trashDisabled={!available.trash}
      notice={<>{forkFailed && <div className="error-banner" role="alert">
        <span>{client.error || "Could not fork this chat. Try again."}</span>
        <button type="button" aria-label="Dismiss fork error" onClick={() => { setForkFailed(false); client.dismissError(); }}>
          <Icon name="close" size={14} /></button>
      </div>}
      {!client.trash.open && <TrashNotice state={client.trash} controller={client.trashController}
        openSession={(id) => void select(id)} blocked={client.busy} openDisabled={false} />}</>}
      settings={client.settings.show} settingsDisabled={!available.settings}
      tools={client.showTools} toolsDisabled={!available.tools}
      designer={() => { close(); client.showDesigner(); }} designerDisabled={!available.designer}
      channels={client.channels.show} channelsDisabled={!available.channels} demo={!!sessions.config?.demo}
    />
    {renaming && <SessionTitleDialog key={renaming.id} session={renaming} error={client.error}
      close={() => setRenaming(undefined)} save={title => client.renameSession(renaming.id, title)} />}
    <button type="button" className="sidebar-divider-toggle"
      aria-label={collapsed ? "Expand sessions sidebar" : "Collapse sessions sidebar"}
      title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
      aria-controls="session-navigation" aria-expanded={!collapsed} onClick={toggleCollapsed}>
      <Icon name="chevron" size={14} />
    </button>
  </>;
}
