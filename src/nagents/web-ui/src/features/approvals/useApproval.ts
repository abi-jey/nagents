import { useRef, useState } from "react";
import { request } from "../../api/client";
import { text } from "../../api/events";
import type { Approval, Decision, WireEvent } from "../../types";

export function useApproval(token: string) {
  const [pending, setPending] = useState<Approval>();
  const [deciding, setDeciding] = useState(false);
  const [error, setError] = useState("");
  const current = useRef<Approval | undefined>(undefined);
  const decisionFor = useRef("");

  function open(event: WireEvent) {
    const approval = {
      run_id: text(event, "run_id"),
      approval_id: text(event, "approval_id"),
      call_id: text(event, "id"),
      tool: text(event, "tool"),
      description: text(event, "description"),
      preview: text(event, "preview"),
      arguments: event.arguments,
    };
    current.current = approval;
    setPending(approval);
    setError("");
  }

  function close(id = "") {
    if (id && current.current?.approval_id !== id) return;
    current.current = undefined;
    setPending(undefined);
    setError("");
  }

  async function decide(decision: Decision) {
    const approval = current.current;
    if (!approval || decisionFor.current || !token) return;
    decisionFor.current = approval.approval_id;
    setDeciding(true);
    setError("");
    try {
      await request("approval", token, {
        run_id: approval.run_id,
        approval_id: approval.approval_id,
        call_id: approval.call_id,
        decision,
      });
      // A late HTTP acknowledgement must not dismiss the next streamed approval.
      close(approval.approval_id);
    } catch (cause) {
      if (current.current?.approval_id === approval.approval_id) {
        setError(
          cause instanceof Error
            ? cause.message
            : "Decision not confirmed. Cancel the run if disconnected.",
        );
      }
    } finally {
      decisionFor.current = "";
      setDeciding(false);
    }
  }

  return { pending, deciding, error, open, close, decide };
}
