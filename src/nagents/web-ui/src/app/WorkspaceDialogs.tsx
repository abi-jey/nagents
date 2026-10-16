import { CommandHelp } from "../features/chat/CommandHelp.js";
import { SkillsDialog } from "../features/chat/SkillsDialog.js";
import { skillCommand } from "../features/chat/commands.js";
import { LoginDialog } from "../features/settings/LoginDialog.js";
import "../features/chat/commands.css";
import { ApprovalReview } from "../features/approvals/ApprovalReview.js";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { ChannelsDialog } from "../features/channels/ChannelsDialog";
import { DeleteSessionDialog } from "../features/sessions/DeleteSessionDialog";
import { TrashDialog } from "../features/sessions/TrashDialog";
import { ToolsDialog } from "../features/tools/ToolsDialog";
import "../features/sessions/trash.css";
import "../features/settings/settings.css";
import "../features/settings/workspacePanel.css";
import "../features/channels/channels.css";
import type { Client } from "./useClient";
import { SessionAgentPicker } from "../features/sessions/SessionAgentPicker.js";

export function WorkspaceDialogs({ client, select, closePanel }: {
  client: Client; select: (id?: string) => Promise<void>; closePanel: () => void;
}) {
  const { sessions, chat } = client;
  const config = sessions.config;
  function openRestored(id: string) { client.trashController.close(); void select(id); }
  return <>
    {client.commandHelp && <CommandHelp close={client.closeCommandHelp} />}
    {client.skills.open && <SkillsDialog skills={client.skills} choose={skill => {
      client.chat.setPrompt(`/${skillCommand(skill)} `); client.skills.close();
      requestAnimationFrame(() => document.getElementById("composer")?.focus());
    }} />}
    {client.loginOpen && config && <LoginDialog token={config.token} close={client.closeLogin} applied={client.connect} />}
    {client.panel === "tools" && config && <ToolsDialog token={config.token} blocked={false} close={closePanel} />}
    {client.settings.open && <SettingsDialog settings={client.settings} />}
    {client.sessionAgent.open && <SessionAgentPicker {...client.sessionAgent} />}
    {client.trash.open && <TrashDialog state={client.trash} controller={client.trashController}
      permanent={(item) => client.deletionController.showTrash(item)} openSession={openRestored}
       blocked={client.operating} openDisabled={false} />}
    {client.deletion.target && <DeleteSessionDialog state={client.deletion} controller={client.deletionController} currentSessionId={sessions.sessionId} />}
    {client.channels.open && <ChannelsDialog channels={client.channels} sessions={sessions.sessions} selected={sessions.sessionId} />}
    <ApprovalReview sessionId={sessions.sessionId} pending={chat.approval.pending}
      busy={chat.approval.deciding} error={chat.approval.error} decide={(decision) => void chat.approval.decide(decision)}
      cancel={() => void client.cancel()} />
  </>;
}
