import { useState, type RefObject } from "react";
import { Icon } from "../components/Icon";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { TrashNotice } from "../features/sessions/TrashDialog";
import { restoreDeletionFocus } from "../api/deletion";
import type { Client } from "./useClient";
import { useChatFolders } from "../features/sessions/useChatFolders.js";
import { SessionTitleDialog, type SessionTitleAction } from "../features/sessions/SessionTitleDialog.js";

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
  const [titleAction, setTitleAction] = useState<SessionTitleAction>();
  const folders = useChatFolders(sessions.config?.token || "", sessions.sessions.map(session => session.id));
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
      select={(id) => void select(id)} remove={(session) => void moveToTrash(session)}
      permanent={(session) => client.deletionController.show(session)}
      rename={(session) => { client.dismissError(); setTitleAction({ kind: "rename", session }); }}
      fork={(session) => { client.dismissError(); setTitleAction({ kind: "fork", session }); }} canFork={client.canForkSession}
      canDelete={(id) => !client.deletion.target && client.canDeleteSession(id)}
      trash={client.trashController.show} trashDisabled={!available.trash}
      notice={!client.trash.open && <TrashNotice state={client.trash} controller={client.trashController}
        openSession={(id) => void select(id)} blocked={client.busy} openDisabled={false} />}
      settings={client.settings.show} settingsDisabled={!available.settings}
      tools={client.showTools} toolsDisabled={!available.tools}
      designer={() => { close(); client.showDesigner(); }} designerDisabled={!available.designer}
      channels={client.channels.show} channelsDisabled={!available.channels} demo={!!sessions.config?.demo}
    />
    {titleAction && <SessionTitleDialog key={`${titleAction.kind}:${titleAction.session.id}`} action={titleAction} error={client.error}
      close={() => setTitleAction(undefined)} save={async title => {
        const saved = titleAction.kind === "rename" ? await client.renameSession(titleAction.session.id, title)
          : await client.forkSession(titleAction.session.id, title);
        if (saved && titleAction.kind === "fork") {
          close(); requestAnimationFrame(() => composer.current?.focus({ preventScroll: true }));
        }
        return saved;
      }} />}
    <button type="button" className="sidebar-divider-toggle"
      aria-label={collapsed ? "Expand sessions sidebar" : "Collapse sessions sidebar"}
      title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
      aria-controls="session-navigation" aria-expanded={!collapsed} onClick={toggleCollapsed}>
      <Icon name="chevron" size={14} />
    </button>
  </>;
}
