import { TreeActions, type TreeAction } from "./TreeActions.js";
import type { Session } from "../../types.js";
export { menuPosition, menuKey, handleMenuKey } from "./TreeActions.js";

export function SessionMenu({ session, disabled, permanent, move, rename, fork, forkDisabled = false }: {
  session: Session; disabled: boolean; permanent: (session: Session) => void; move?: () => void;
  rename?: () => void; fork?: () => void; forkDisabled?: boolean;
}) {
  const items: TreeAction[] = [];
  if (rename) items.push({ label: "Rename chat…", icon: "edit", run: rename });
  if (fork) items.push({ label: "Fork chat", icon: "branch", disabled: forkDisabled || !!session.active_run_id, run: fork });
  if (move) items.push({ label: "Move to folder…", icon: "folder", run: move });
  items.push({ label: "Delete forever…", icon: "trash", danger: true, disabled, run: () => permanent(session) });
  return <TreeActions label={session.title || "New session"} items={items} />;
}
