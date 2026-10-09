import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { ApprovalDialog } from "./ApprovalDialog.js";
import type { Approval, Decision } from "../../types.js";

export function ApprovalReview({ sessionId, pending, busy, error, decide, cancel }: {
  sessionId: string; pending?: Approval; busy: boolean; error: string;
  decide: (decision: Decision) => void; cancel: () => void;
}) {
  const [deferred, defer] = useState("");
  useEffect(() => { defer(""); }, [sessionId]);
  if (!pending) return null;
  if (deferred === pending.approval_id) {
    const notice = <div className="activity-banner" role="status">
      <span>Approval waiting in this chat.</span>
      <button aria-label="Review pending approval" onClick={() => defer("")}>Review</button>
    </div>;
    const target = typeof document === "undefined" ? null : document.getElementById("approval-waiting-slot");
    return target ? createPortal(notice, target) : notice;
  }
  return <ApprovalDialog key={pending.approval_id} approval={pending} busy={busy} error={error} decide={decide}
    later={() => defer(pending.approval_id)} cancel={cancel} />;
}
