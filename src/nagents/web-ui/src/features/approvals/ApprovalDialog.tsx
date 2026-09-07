import { useEffect, useRef } from "react";
import { preview } from "../../api/events";
import type { Approval, Decision } from "../../types";

export function ApprovalDialog({
  approval,
  busy,
  error,
  decide,
  cancel,
}: {
  approval: Approval;
  busy: boolean;
  error: string;
  decide: (decision: Decision) => void;
  cancel: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const deny = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element?.showModal();
    deny.current?.focus();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, []);

  return (
    <dialog
      ref={dialog}
      aria-labelledby="approval-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) decide("deny");
      }}
    >
      <div className="eyebrow">Human approval / one call only</div>
      <h2 id="approval-title">Allow {approval.tool}?</h2>
      <p>{approval.description}</p>
      <dl>
        <dt>Call</dt>
        <dd>{approval.call_id}</dd>
      </dl>
      <pre className="approval-preview">
        {approval.preview || preview(approval.arguments)}
      </pre>
      {approval.preview && (
        <details>
          <summary>Tool arguments</summary>
          <pre>{preview(approval.arguments)}</pre>
        </details>
      )}
      <p className="muted">
        Review the exact operation. Local tools are not sandboxed. Deny is the
        default; Escape denies this call.
      </p>
      {error && (
        <p role="alert" className="error-text">
          {error}
        </p>
      )}
      <div className="dialog-actions">
        <button className="cancel" onClick={cancel}>
          Stop run
        </button>
        <button ref={deny} disabled={busy} onClick={() => decide("deny")}>
          Deny
        </button>
        <button
          className="primary"
          disabled={busy}
          onClick={() => decide("allow")}
        >
          Allow Once
        </button>
      </div>
    </dialog>
  );
}
