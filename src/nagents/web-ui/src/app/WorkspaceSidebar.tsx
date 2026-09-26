import type { RefObject } from "react";
import { Icon } from "../components/Icon";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { TrashNotice } from "../features/sessions/TrashDialog";
import { restoreDeletionFocus } from "../api/deletion";
import type { Client } from "./useClient";

export function WorkspaceSidebar({ client, composer, open, close, select, collapsed, toggleCollapsed }: {
  client: Client;
  composer: RefObject<HTMLTextAreaElement | null>;
  open: boolean;
  close: () => void;
  select: (id?: string) => Promise<void>;
  collapsed: boolean;
  toggleCollapsed: () => void;
}) {
  const { sessions, available, dictation } = client;
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
      disabled={!available.navigate} newDisabled={!available.create} open={open} close={close}
      select={(id) => void select(id)} remove={(session) => void moveToTrash(session)}
      permanent={(session) => client.deletionController.show(session)}
      canDelete={(id) => !client.deletion.target && client.canDeleteSession(id)}
      trash={client.trashController.show} trashDisabled={!available.trash}
      notice={!client.trash.open && <TrashNotice state={client.trash} controller={client.trashController}
        openSession={(id) => void select(id)} blocked={client.busy || (dictation.unfinished && dictation.state.phase !== "review")}
        openDisabled={dictation.unfinished} />}
      settings={client.settings.show} globalSettings={client.settings.showGlobal} settingsDisabled={!available.settings}
      tools={client.showTools} toolsDisabled={!available.tools}
      designer={() => { close(); client.showDesigner(); }} designerDisabled={!available.designer}
      channels={client.channels.show} channelsDisabled={!available.channels} demo={!!sessions.config?.demo}
    />
    <button type="button" className="sidebar-divider-toggle"
      aria-label={collapsed ? "Expand sessions sidebar" : "Collapse sessions sidebar"}
      title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
      aria-controls="session-navigation" aria-expanded={!collapsed} onClick={toggleCollapsed}>
      <Icon name="chevron" size={14} />
    </button>
  </>;
}
