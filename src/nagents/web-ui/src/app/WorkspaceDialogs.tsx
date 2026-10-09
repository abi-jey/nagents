import { CommandHelp } from "../features/chat/CommandHelp.js";
import { LoginDialog } from "../features/settings/LoginDialog.js";
import "../features/chat/commands.css";
import { ApprovalReview } from "../features/approvals/ApprovalReview.js";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { ChannelsDialog } from "../features/channels/ChannelsDialog";
import { DeleteSessionDialog } from "../features/sessions/DeleteSessionDialog";
import { TrashDialog } from "../features/sessions/TrashDialog";
import { Designer } from "../features/designer/Designer";
import { ToolsDialog } from "../features/tools/ToolsDialog";
import "../features/sessions/trash.css";
import "../features/settings/settings.css";
import "../features/channels/channels.css";
import type { Client } from "./useClient";

export function WorkspaceDialogs({ client, select, closePanel }: {
  client: Client; select: (id?: string) => Promise<void>; closePanel: () => void;
}) {
  const { sessions, chat } = client;
  const config = sessions.config;
  function openRestored(id: string) { client.trashController.close(); void select(id); }
  return <>
    {client.commandHelp && <CommandHelp close={client.closeCommandHelp} />}
    {client.loginOpen && config && <LoginDialog token={config.token} close={client.closeLogin} applied={client.connect} />}
    {client.panel === "designer" && config && <Designer token={config.token} configureChannels={client.channels.show} close={closePanel} />}
    {client.panel === "tools" && config && <ToolsDialog token={config.token} blocked={client.busy} close={closePanel} />}
    {client.settings.open && <SettingsDialog settings={client.settings} />}
    {client.trash.open && <TrashDialog state={client.trash} controller={client.trashController}
      permanent={(item) => client.deletionController.showTrash(item)} openSession={openRestored}
       blocked={client.busy} openDisabled={false} />}
    {client.deletion.target && <DeleteSessionDialog state={client.deletion} controller={client.deletionController} currentSessionId={sessions.sessionId} />}
    {client.channels.open && <ChannelsDialog channels={client.channels} sessions={sessions.sessions} selected={sessions.sessionId} />}
    <ApprovalReview sessionId={sessions.sessionId} pending={chat.approval.pending}
      busy={chat.approval.deciding} error={chat.approval.error} decide={(decision) => void chat.approval.decide(decision)}
      cancel={() => void client.cancel()} />
  </>;
}
