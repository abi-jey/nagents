import { useEffect, useRef } from "react";
import { webCommands } from "./commands.js";

export function CommandHelp({ close }: { close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); if (previous instanceof HTMLElement) previous.focus(); };
  }, []);
  return <dialog className="workspace-dialog command-help" ref={dialog} aria-labelledby="command-help-title"
    onCancel={event => { event.preventDefault(); close(); }}>
    <header><h2 id="command-help-title">Browser commands</h2><button type="button" onClick={close} aria-label="Close commands">×</button></header>
    <p>Type / in the composer to choose a command. Commands act on this workspace; they are not sent as chat messages.</p>
    <dl>{webCommands.map(command => <div key={command.name}><dt><code>/{command.name} {command.argument}</code></dt><dd>{command.description}</dd></div>)}</dl>
    <p><code>/resume</code> is an alias for <code>/sessions</code>. Terminal-only and plugin commands are not available in the browser.</p>
  </dialog>;
}
