import { useId, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import type { Session } from "../../types.js";

export function menuPosition(anchor: { left: number; right: number; top: number; bottom: number }, width: number, height: number) {
  return { left: Math.max(8, Math.min(anchor.right - 212, width - 220)),
    top: Math.max(8, anchor.bottom + 68 <= height ? anchor.bottom + 4 : anchor.top - 60) };
}
export function menuKey(key: string): "focus" | "close" | "tab" | "" {
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(key)) return "focus";
  return key === "Escape" ? "close" : key === "Tab" ? "tab" : "";
}
export function handleMenuKey(event: Pick<KeyboardEvent, "key" | "stopPropagation" | "preventDefault">,
  focus: () => void, close: () => void): void {
  const command = menuKey(event.key);
  if (!command) return;
  // Stop before hiding the popover: the drawer's document handler must not see
  // an Escape whose menu has already disappeared. Tab keeps native traversal.
  event.stopPropagation();
  if (command !== "tab") event.preventDefault();
  if (command === "focus") focus(); else close();
}
export function SessionMenu({ session, disabled, permanent }: { session: Session; disabled: boolean; permanent: (session: Session) => void }) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const action = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  function show() {
    if (!trigger.current || !menu.current || disabled) return;
    const position = menuPosition(trigger.current.getBoundingClientRect(), innerWidth, innerHeight);
    menu.current.style.left = `${position.left}px`; menu.current.style.top = `${position.top}px`;
    menu.current.showPopover(); setOpen(true); action.current?.focus();
  }
  function close() { menu.current?.hidePopover(); setOpen(false); trigger.current?.focus({ preventScroll: true }); }
  return <>
    <button ref={trigger} className="session-more" aria-label={`More actions: ${session.title || "New session"}`}
      title="More actions" aria-haspopup="menu" aria-controls={id} aria-expanded={open} disabled={disabled}
      onClick={() => open ? close() : show()}
      onKeyDown={(event) => { if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); show(); } }}>
      <Icon name="more" size={15} />
    </button>
    <div ref={menu} id={id} className="session-menu" role="menu" aria-label={`Actions for ${session.title || "New session"}`}
      popover="auto" onToggle={(event) => setOpen(event.newState === "open")}
      onKeyDown={(event) => handleMenuKey(event, () => action.current?.focus(), close)}>
      <button ref={action} role="menuitem" className="danger-action" disabled={disabled} onClick={() => { close(); permanent(session); }}>
        <Icon name="trash" size={16} /> Delete forever…
      </button>
    </div>
  </>;
}
