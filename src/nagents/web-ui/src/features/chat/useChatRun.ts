import { useEffect, useEffectEvent, useRef, useState } from "react";
import { request } from "../../api/client";
import { pollActivity } from "../../api/activity";
import { readEvents, text } from "../../api/events";
import type { ActivityReply, Snapshot } from "../../types";
import { useApproval } from "../approvals/useApproval";
import {
  appendActivity,
  appendEvent,
  fromHistory,
  type Entry,
} from "./transcript";

export function useChatRun(
  token: string,
  sessionId: string,
  activityCursor: number,
  initialBackgroundRunId: string,
) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [prompt, setPrompt] = useState("");
  const [runId, setRunId] = useState("");
  const [status, setStatus] = useState("Connecting to local harness");
  const [background, setBackground] = useState({ sessionId: "", runId: "" });
  const backgroundRunId =
    background.sessionId === sessionId
      ? background.runId
      : initialBackgroundRunId;
  const [activityError, setActivityError] = useState("");
  const [pendingWakeups, setPendingWakeups] = useState(0);
  const [transcriptVersion, setTranscriptVersion] = useState(0);
  const [stopping, setStopping] = useState(false);
  const stream = useRef<AbortController | null>(null);
  const cancelling = useRef(false);
  const approval = useApproval(token);

  useEffect(() => () => stream.current?.abort(), []);

  const receiveActivity = useEffectEvent((reply: ActivityReply) => {
    if (reply.session_id !== sessionId) return;
    setActivityError("");
    setBackground({
      sessionId,
      runId: stream.current ? "" : reply.active_run_id,
    });
    setPendingWakeups(reply.pending_wakeups.length);
    // Background runs are unattended. Their evidence never opens an approval modal.
    setEntries((current) => appendActivity(current, reply));
    if (!stream.current) {
      const finished = reply.events.findLast(
        (event) => event.event === "run_finished",
      );
      if (finished)
        setStatus(
          finished.status === "completed"
            ? "Ready"
            : `Background run ${text(finished, "status")}. Completed actions were not rolled back.`,
        );
      else if (backgroundRunId && !reply.active_run_id)
        setStatus(
          "Run no longer active. No final result event was recorded in this connection.",
        );
    }
  });
  const activityFailed = useEffectEvent((message: string) =>
    setActivityError(message),
  );
  useEffect(() => {
    setBackground({ sessionId, runId: initialBackgroundRunId });
    setActivityError("");
    setPendingWakeups(0);
    if (!token || !sessionId) return;
    const controller = new AbortController();
    void pollActivity({
      token,
      sessionId,
      cursor: activityCursor,
      signal: controller.signal,
      receive: (reply) => receiveActivity(reply),
      failed: (message) => activityFailed(message),
    });
    return () => controller.abort();
  }, [token, sessionId, activityCursor, initialBackgroundRunId]);

  function loadHistory(snapshot: Snapshot) {
    setEntries(fromHistory(snapshot));
    setTranscriptVersion((version) => version + 1);
    approval.close();
    setStatus("Ready");
  }

  async function submit(value: string) {
    if (stream.current || backgroundRunId)
      throw new Error("A run is already active.");
    const controller = new AbortController();
    stream.current = controller;
    setStatus("Starting run");
    let started = false;
    let finished = false;
    let activeId = "";
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
      setEntries((current) =>
        appendEvent(current, { event: "user_message", text: value }),
      );
      await readEvents(response.body, (event) => {
        if (event.event === "run_started") {
          started = true;
          activeId = text(event, "run_id");
          setRunId(activeId);
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
        if (event.event === "run_finished") {
          finished = true;
          approval.close();
          setStatus(
            text(event, "status") === "completed"
              ? "Ready"
              : `Run ${text(event, "status")}. Completed actions were not rolled back.`,
          );
        }
        setEntries((current) => appendEvent(current, event));
      });
      if (!finished)
        throw new Error(
          "Stream disconnected. Partial output is kept; completed actions were not rolled back. Reconnect before another prompt.",
        );
    } catch (cause) {
      controller.abort();
      if (started && !finished)
        setEntries((current) =>
          appendEvent(current, {
            event: "client_disconnected",
            run_id: activeId,
          }),
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
    }
  }

  async function cancel(id: string) {
    if (!id || cancelling.current) return;
    cancelling.current = true;
    setStopping(true);
    setStatus("Cancelling and waiting for tools to stop");
    try {
      await request("cancel", token, { run_id: id });
    } catch (cause) {
      stream.current?.abort();
      throw cause;
    } finally {
      cancelling.current = false;
      setStopping(false);
    }
  }

  return {
    entries,
    prompt,
    setPrompt,
    runId: runId || backgroundRunId,
    backgroundRunId,
    activityError,
    pendingWakeups,
    transcriptVersion,
    status:
      runId || stopping
        ? status
        : backgroundRunId
          ? "Run active outside this connection"
          : status,
    setStatus,
    approval,
    loadHistory,
    submit,
    cancel,
  };
}
