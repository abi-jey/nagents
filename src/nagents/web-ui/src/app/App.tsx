import { useCallback, useRef, useState } from "react";
import { Conversation } from "../features/chat/Conversation";
import { useClient } from "./useClient";
import { WorkspaceHeader } from "./WorkspaceHeader";
import { WorkspaceSidebar } from "./WorkspaceSidebar";
import { WorkspaceComposer } from "./WorkspaceComposer";
import { WorkspaceDialogs } from "./WorkspaceDialogs";

export function App() {
  const client = useClient();
  const [navOpen, setNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const closeNavigation = useCallback(() => setNavOpen(false), []);
  const composer = useRef<HTMLTextAreaElement>(null);

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
  function closePanel() {
    const fromDesigner = client.panel === "designer";
    client.closePanel();
    if (fromDesigner) requestAnimationFrame(() => composer.current?.focus());
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#composer">Skip to prompt</a>
      <main inert={client.panel === "designer"} className={sidebarCollapsed ? "sidebar-collapsed" : undefined}>
        <WorkspaceHeader client={client} navOpen={navOpen} toggleNavigation={() => setNavOpen(!navOpen)} />
        <WorkspaceSidebar client={client} composer={composer} open={navOpen} close={closeNavigation}
          select={select} collapsed={sidebarCollapsed} toggleCollapsed={() => setSidebarCollapsed(!sidebarCollapsed)} />
        <Conversation token={client.sessions.config?.token || ""} key={client.sessions.sessionId}
          entries={client.chat.entries} sessionId={client.sessions.sessionId} demo={!!client.sessions.config?.demo}
          canSubmit={client.available.submit} submit={(value) => void submit(value)} />
        <WorkspaceComposer client={client} composer={composer} submit={() => void submit()} select={select} />
      </main>
      <WorkspaceDialogs client={client} select={select} closePanel={closePanel} />
    </div>
  );
}
