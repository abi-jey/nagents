import { useId, useRef, useState, type ComponentProps } from "react";
import { Icon } from "../../components/Icon.js";

export function menuPosition(anchor: { left: number; right: number; top: number; bottom: number }, width: number, height: number, menuHeight = 64) {
  return { left: Math.max(8, Math.min(anchor.right - 212, width - 220)),
    top: Math.max(8, anchor.bottom + menuHeight + 4 <= height ? anchor.bottom + 4 : anchor.top - menuHeight + 4) };
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
export type TreeAction = { label: string; icon: ComponentProps<typeof Icon>["name"]; disabled?: boolean; danger?: boolean; run(): void };

export function TreeActions({ label, items, className = "session-more" }: { label: string; items: TreeAction[]; className?: string }) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const disabled = items.every(item => item.disabled);
  function show() {
    if (!trigger.current || !menu.current || disabled) return;
    menu.current.showPopover();
    const position = menuPosition(trigger.current.getBoundingClientRect(), innerWidth, innerHeight,
      menu.current.getBoundingClientRect().height || 64);
    menu.current.style.left = `${position.left}px`; menu.current.style.top = `${position.top}px`;
    setOpen(true); menu.current.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus({ preventScroll: true });
  }
  function close() { menu.current?.hidePopover(); setOpen(false); trigger.current?.focus({ preventScroll: true }); }
  return <>
    <button ref={trigger} className={className} aria-label={`More actions: ${label}`}
      title="More actions" aria-haspopup="menu" aria-controls={id} aria-expanded={open} disabled={disabled}
      onClick={() => open ? close() : show()}
      onKeyDown={(event) => { if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); show(); } }}>
      <Icon name="more" size={15} />
    </button>
    <div ref={menu} id={id} className="session-menu" role="menu" aria-label={`Actions for ${label}`}
      popover="auto" onToggle={(event) => setOpen(event.newState === "open")}
      onKeyDown={(event) => handleMenuKey(event, () => {
        const controls = [...menu.current!.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
        const current = controls.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? controls.length - 1
          : (current + (event.key === "ArrowUp" ? -1 : 1) + controls.length) % controls.length;
        controls[next]?.focus({ preventScroll: true });
      }, close)}>
      {items.map(item => <button key={item.label} role="menuitem" disabled={item.disabled}
        className={item.danger ? "danger-action" : undefined} onClick={() => { close(); item.run(); }}>
        <Icon name={item.icon} size={16} /> {item.label}
      </button>)}
    </div>
  </>;
}
