import type { ComponentProps, ReactNode } from "react";
import { Conversation } from "../features/chat/Conversation.js";
import { TeamPane } from "../features/team/TeamPane.js";

/** Distinct sibling identities keep polling from orphaning either reading surface. */
export function WorkspaceActivity({ sessionId, conversation, task, composer, team }: {
  sessionId: string;
  conversation: ComponentProps<typeof Conversation>;
  task?: ReactNode;
  composer: ReactNode;
  team: ComponentProps<typeof TeamPane>;
}) {
  return <>{task}<Conversation key={`conversation:${sessionId}`} {...conversation} />{composer}<TeamPane key={`team:${sessionId}`} {...team} /></>;
}
