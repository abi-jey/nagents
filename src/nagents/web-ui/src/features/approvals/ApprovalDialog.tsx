import { useEffect, useRef } from "react";
import { preview } from "../../api/events.js";
import { CodeBlock } from "../../components/CodeBlock.js";
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
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element?.showModal();
    heading.current?.focus();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, []);

  return (
    <dialog
      ref={dialog}
      className="approval-dialog"
      aria-labelledby="approval-title"
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const controls = [
          ...event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not(:disabled), [tabindex="0"]',
          ),
        ];
        const first = controls[0];
        const last = controls.at(-1);
        if (
          event.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === heading.current)
        ) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) decide("deny");
      }}
    >
      <header className="approval-heading">
        <h2 id="approval-title" ref={heading} tabIndex={-1}>
          Review <code>{approval.tool}</code>
        </h2>
        <p>One call only. Deny is the default.</p>
      </header>
      <div className="approval-body">
        <dl className="request-identity">
          <dt>Call</dt>
          <dd>{approval.call_id}</dd>
          {approval.task_id && (
            <>
              <dt>Task</dt>
              <dd>
                {approval.task_name} ({approval.task_id}), depth{" "}
                {approval.depth}, activation {approval.activation}
              </dd>
            </>
          )}
        </dl>
        <p className="approval-context">{approval.description}</p>
        <CodeBlock label="Exact inputs" text={preview(approval.arguments)} />
        {approval.preview && (
          <CodeBlock label="Proposed change" text={approval.preview} />
        )}
        <p className="muted">
          Review the inputs and proposed change, not just the description. Local
          tools are not sandboxed. Escape denies this call.
        </p>
        {error && (
          <p role="alert" className="error-text">
            {error}
          </p>
        )}
      </div>
      <div className="dialog-actions">
        <button className="deny" disabled={busy} onClick={() => decide("deny")}>
          Deny
        </button>
        <button disabled={busy} onClick={() => decide("allow")}>
          Allow once
        </button>
        <button className="cancel" onClick={cancel}>
          Stop run
        </button>
      </div>
    </dialog>
  );
}
