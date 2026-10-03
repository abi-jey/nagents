import { useLayoutEffect, useRef } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";

/** A top-layer settings surface; the voice controller remains mounted outside it. */
export function VoiceSettingsDialog({ open, saving, close, children }: {
  open: boolean; saving: boolean; close(): void; children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const backdropPress = useRef(false);
  useLayoutEffect(() => {
    const element = dialog.current;
    if (!open || !element) return;
    const previous = document.activeElement;
    const overflow = document.documentElement.style.overflow;
    document.documentElement.style.overflow = "hidden";
    element.showModal();
    const body = element.querySelector<HTMLElement>(".live-settings-body");
    if (body) body.scrollTop = 0;
    element.querySelector<HTMLElement>("#live-settings-title")?.focus({ preventScroll: true });
    return () => {
      element.close();
      document.documentElement.style.overflow = overflow;
      // Restore after React's commit-time focus preservation. Another settings
      // surface may be taking over (Manage connections), or this one reopening.
      queueMicrotask(() => {
        if (document.querySelector("dialog[open]")) return;
        if (previous instanceof HTMLElement && previous.isConnected && previous !== document.body)
          previous.focus({ preventScroll: true });
        else document.querySelector<HTMLElement>("#voice-session .voice-settings-trigger, .voice-entry-settings")?.focus({ preventScroll: true });
      });
    };
  }, [open]);

  function outside(event: { target: EventTarget; currentTarget: HTMLDialogElement; clientX: number; clientY: number }) {
    const bounds = event.currentTarget.getBoundingClientRect();
    return event.target === event.currentTarget &&
      (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom);
  }

  return createPortal(<dialog ref={dialog} id="voice-settings-dialog" className="voice-settings-dialog"
    aria-labelledby="live-settings-title" aria-describedby="live-settings-description" aria-modal="true"
    onCancel={event => { event.preventDefault(); event.stopPropagation(); if (!saving) close(); }}
    onPointerDown={event => { backdropPress.current = outside(event); }}
    onClick={event => { if (backdropPress.current && outside(event) && !saving) close(); backdropPress.current = false; }}
    onKeyDown={event => {
      if (event.key === "Escape") { event.stopPropagation(); return; }
      if (event.key !== "Tab") return;
      const controls = [...event.currentTarget.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
      )].filter(control => control.getClientRects().length > 0);
      const first = controls[0], last = controls.at(-1);
      const heading = event.currentTarget.querySelector<HTMLElement>("#live-settings-title");
      if (!first) { event.preventDefault(); heading?.focus(); }
      else if (event.shiftKey && (document.activeElement === first || document.activeElement === heading)) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    }}>{children}</dialog>, document.body);
}
