import { useRef } from "react";
import { Icon } from "../../components/Icon.js";
import type { Session } from "../../types.js";
import { SessionMenu } from "./SessionMenu.js";

export function SessionRow({ session, selected, disabled, canDelete, canFork, select, remove, permanent, rename, fork, move }: {
  session: Session; selected: boolean; disabled: boolean; canDelete: boolean; canFork: boolean;
  select: (id: string) => void; remove: (session: Session) => void; permanent: (session: Session) => void;
  rename?: (session: Session) => void; fork?: (session: Session) => void; move?: () => void;
}) {
  const row = useRef<HTMLDivElement>(null);
  return <div ref={row} className={`session-row${selected ? " selected" : ""}`} data-session-id={session.id}>
    <button className={`session${selected ? " selected" : ""}`}
      aria-label={`${session.title || "New session"}${session.active_run_id ? " — Working" : ""}`}
      aria-current={selected ? "page" : undefined} disabled={disabled} onClick={() => select(session.id)}
      title={`${session.title} · Updated ${session.updated_at.slice(0, 10)}`}>
      <Icon name={session.forked_from ? "branch" : "chat"} size={15} />
      <span className="session-title">{session.title || "New session"}</span>
      {session.active_run_id && <span className="session-working" title="Working"><span className="sr-only">Working</span></span>}
      <small className="sr-only">Updated {session.updated_at.slice(0, 10)}</small>
    </button>
    <button className="session-delete" aria-label={`Move to Trash: ${session.title || "New session"}`}
      title="Move to Trash" disabled={!canDelete || !!session.active_run_id}
      onClick={() => remove(session)}><Icon name="trash" size={15} /></button>
    <SessionMenu session={session} disabled={!canDelete || !!session.active_run_id} permanent={permanent} move={move}
      rename={!disabled && rename ? () => rename(session) : undefined}
      fork={fork ? () => fork(session) : undefined} forkDisabled={disabled || !canFork} contextTarget={row} />
  </div>;
}
