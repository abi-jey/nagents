import { ApprovalDialog } from "../features/approvals/ApprovalDialog";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { ChannelsDialog } from "../features/channels/ChannelsDialog";
import { DeleteSessionDialog } from "../features/sessions/DeleteSessionDialog";
import { TrashDialog } from "../features/sessions/TrashDialog";
import { Designer } from "../features/designer/Designer";
import { ToolsDialog } from "../features/tools/ToolsDialog";
import { LiveDialog } from "../features/live/LiveDialog";
import "../features/sessions/trash.css";
import "../features/dictation/dictation.css";
import "../features/settings/settings.css";
import "../features/channels/channels.css";
import type { Client } from "./useClient";

export function WorkspaceDialogs({ client, select, closePanel }: {
  client: Client; select: (id?: string) => Promise<void>; closePanel: () => void;
}) {
  const { sessions, chat, dictation } = client;
  const config = sessions.config;
  function openRestored(id: string) { client.trashController.close(); void select(id); }
  function closeLive() { closePanel(); requestAnimationFrame(() => document.getElementById("live-launch")?.focus()); }
  return <>
    {client.panel === "designer" && config && <Designer token={config.token} configureChannels={client.channels.show} close={closePanel} />}
    {client.panel === "tools" && config && <ToolsDialog token={config.token} blocked={client.busy || dictation.unfinished} close={closePanel} />}
    {client.panel === "live" && config && <LiveDialog key={config.token} token={config.token} close={closeLive} />}
    {client.settings.open && <SettingsDialog settings={client.settings} />}
    {client.trash.open && <TrashDialog state={client.trash} controller={client.trashController}
      permanent={(item) => client.deletionController.showTrash(item)} openSession={openRestored}
      blocked={client.busy || (dictation.unfinished && dictation.state.phase !== "review")} openDisabled={dictation.unfinished} />}
    {client.deletion.target && <DeleteSessionDialog state={client.deletion} controller={client.deletionController} currentSessionId={sessions.sessionId} />}
    {client.channels.open && <ChannelsDialog channels={client.channels} sessions={sessions.sessions} selected={sessions.sessionId} />}
    {chat.approval.pending && <ApprovalDialog key={chat.approval.pending.approval_id} approval={chat.approval.pending}
      busy={chat.approval.deciding} error={chat.approval.error} decide={(decision) => void chat.approval.decide(decision)}
      cancel={() => void client.cancel()} />}
  </>;
}
