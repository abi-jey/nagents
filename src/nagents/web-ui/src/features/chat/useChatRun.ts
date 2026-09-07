import { useEffect, useRef, useState } from "react";
import { request } from "../../api/client";
import { readEvents, text } from "../../api/events";
import type { Snapshot } from "../../types";
import { useApproval } from "../approvals/useApproval";
import { appendEvent, fromHistory, type Entry } from "./transcript";

export function useChatRun(token: string, sessionId: string) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [prompt, setPrompt] = useState("");
  const [runId, setRunId] = useState("");
  const [status, setStatus] = useState("Connecting to local harness");
  const stream = useRef<AbortController | null>(null);
  const cancelling = useRef(false);
  const approval = useApproval(token);

  useEffect(() => () => stream.current?.abort(), []);

  function loadHistory(snapshot: Snapshot) {
    setEntries(fromHistory(snapshot.history));
    approval.close();
    setStatus("Ready");
  }

  async function submit(value: string) {
    if (stream.current) throw new Error("A run is already active.");
    const controller = new AbortController();
    stream.current = controller;
    setStatus("Starting run");
    let started = false;
    let finished = false;
    let compacting = false;
    try {
      const response = await request(
        "run",
        token,
        { session_id: sessionId, prompt: value },
        controller.signal,
      );
      if (!response.body)
        throw new Error("Streaming is unavailable in this browser.");
      setPrompt("");
      setEntries((current) => [
        ...appendEvent(current, { event: "run_finished" }),
        { kind: "user", text: value },
      ]);
      await readEvents(response.body, (event) => {
        if (event.event === "run_started") {
          started = true;
          setRunId(text(event, "run_id"));
          setStatus("Working");
        }
        if (event.event === "approval") {
          approval.open(event);
          setStatus("Waiting for approval");
        }
        if (event.event === "approval_closed") {
          approval.close(text(event, "approval_id"));
          setStatus("Working");
        }
        if (event.event === "compaction_started") compacting = true;
        if (event.event === "compaction_done") compacting = false;
        if (event.event === "run_finished") {
          finished = true;
          approval.close();
          setStatus(
            text(event, "status") === "completed"
              ? "Ready"
              : `Run ${text(event, "status")}. Completed actions were not rolled back.`,
          );
        }
        if (!compacting || !["text_chunk", "text_done"].includes(event.event)) {
          setEntries((current) => appendEvent(current, event));
        }
      });
      if (!finished)
        throw new Error(
          "Stream disconnected. Partial output is kept; completed actions were not rolled back. Reconnect before another prompt.",
        );
    } catch (cause) {
      controller.abort();
      if (started && !finished)
        setEntries((current) =>
          appendEvent(current, { event: "client_disconnected" }),
        );
      setStatus(
        started && !finished
          ? "Disconnected / partial output retained"
          : "Request failed",
      );
      throw cause;
    } finally {
      stream.current = null;
      setRunId("");
      approval.close();
      setEntries((current) => appendEvent(current, { event: "run_finished" }));
    }
  }

  async function cancel(id: string) {
    if (!id || cancelling.current) return;
    cancelling.current = true;
    setStatus("Cancelling and waiting for tools to stop");
    try {
      await request("cancel", token, { run_id: id });
    } catch (cause) {
      stream.current?.abort();
      throw cause;
    } finally {
      cancelling.current = false;
    }
  }

  return {
    entries,
    prompt,
    setPrompt,
    runId,
    status,
    setStatus,
    approval,
    loadHistory,
    submit,
    cancel,
  };
}
