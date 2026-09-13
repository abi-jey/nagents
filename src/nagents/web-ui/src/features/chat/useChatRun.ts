import { useEffect, useEffectEvent, useRef, useState, type SetStateAction } from "react";
import { insertDraft } from "../dictation/draft";
import { request } from "../../api/client";
import { text } from "../../api/events";
import { MessageQueue, queueMessage, queuedMessageFailure } from "../../api/messages";
import { subscribeEvents, type EventFrame } from "../../api/subscription";
import { readBootstrap } from "../../api/bootstrap";
import type { Bootstrap, Snapshot } from "../../types";
import { useApproval } from "../approvals/useApproval";
import { applySnapshot, LiveSessions, pendingApprovals, type LiveTranscript } from "./liveTranscript";

export function useChatRun(token: string, sessionId: string, receive: (frame: EventFrame) => void, acceptCredentials: (bootstrap: Bootstrap) => void) {
  const latestToken = useRef(token);
  latestToken.current = token;
  const authenticated = !!token;
  const cache = useRef(new LiveSessions());
  const queue = useRef(new MessageQueue());
  const [view, setView] = useState<LiveTranscript>({ entries: [] });
  const [prompt, setPromptState] = useState("");
  const latestPrompt = useRef("");
  const draftRevision = useRef(0);
  const selected = useRef(sessionId);
  selected.current = sessionId;
  const [status, setStatus] = useState("Connecting to local harness");
  const [activityError, setActivityError] = useState("");
  const [connected, setConnected] = useState(false);
  const [connectionVersion, setConnectionVersion] = useState(0);
  const [suspended, setSuspended] = useState(false);
  const stopSubscription = useRef<(() => void) | undefined>(undefined);
  const [stopping, setStopping] = useState(false);
  const cancelling = useRef(false);
  const sending = useRef(false);
  const approval = useApproval(token);
  const needsApprovalSync = useRef(true);

  function setPrompt(update: SetStateAction<string>) {
    draftRevision.current++;
    latestPrompt.current = typeof update === "function" ? update(latestPrompt.current) : update;
    setPromptState(latestPrompt.current);
  }
  function insertDictation(value: string): string {
    let error = "";
    setPrompt((current) => {
      const result = insertDraft(current, value, sessionId);
      if (result.ok) {
        error = queuedMessageFailure(result.prompt, sessionId);
        return error ? current : result.prompt;
      }
      error = result.error;
      return current;
    });
    return error;
  }
  const receiveFrame = useEffectEvent((frame: EventFrame) => {
    receive(frame);
    if ((frame.type !== "snapshot" && frame.type !== "event") || frame.session_id !== selected.current) return;
    const previousRun = cache.current.get(frame.session_id).activeRun;
    const next = cache.current.receive(frame);
    setView(next);
    const pending = pendingApprovals(next.activeRun)[0];
    if (needsApprovalSync.current || frame.type === "snapshot" || ["approval", "approval_closed", "run_finished"].includes(frame.record.event)) {
      if (pending) {
        if (approval.pending?.approval_id !== pending.approval_id) approval.open(pending);
      } else approval.close();
      needsApprovalSync.current = false;
    }
    if (frame.type === "event" && frame.record.event === "run_finished") {
      const outcome = text(frame.record, "status");
      setStatus(outcome === "completed" ? "Ready" : `Run ${outcome}. Completed actions were not rolled back.`);
    } else if (frame.type === "snapshot" && previousRun && !next.activeRun && !frame.snapshot.active_run)
      setStatus("Run is no longer active. Its final outcome was not received; saved history is reconciled.");
    else setStatus(pending ? "Waiting for approval" : next.activeRun ? "Working" : next.entries.some((entry) => entry.queued) ? "Message queued" : "Ready");
  });
  const connectionStatus = useEffectEvent((state: "connecting" | "connected" | "reconnecting") => {
    setConnected(state === "connected");
    setActivityError(state === "reconnecting" ? "Live updates disconnected. Reconnecting automatically; accepted messages continue on the server." : "");
    // A stale approval is not actionable until replay/snapshot has reconciled it.
    if (state !== "connected") { needsApprovalSync.current = true; approval.close(); }
  });
  const credentialsRefreshed = useEffectEvent((bootstrap: Bootstrap) => {
    latestToken.current = bootstrap.token;
    acceptCredentials(bootstrap);
  });
  useEffect(() => {
    setView(cache.current.get(sessionId));
    setConnected(false); needsApprovalSync.current = true; approval.close(); setActivityError("");
    if (!authenticated || !sessionId || suspended) return;
    const controller = new AbortController();
    const stop = subscribeEvents({
      token: latestToken.current, sessionId, position: cache.current.get(sessionId).position, url: window.location.href,
      signal: controller.signal, receive: receiveFrame, status: connectionStatus,
      refreshCredentials: readBootstrap, credentials: credentialsRefreshed,
    });
    stopSubscription.current = stop;
    return () => { controller.abort(); stop(); };
    // This transport owns automatic token rotation. Publishing a refreshed token
    // to HTTP consumers must not tear down its new socket or reset its backoff.
    // Explicit full connections still pause/restart through connectionVersion.
  }, [authenticated, sessionId, connectionVersion, suspended]);

  function loadHistory(snapshot: Snapshot) {
    const next = cache.current.set(snapshot.session_id, { ...applySnapshot(cache.current.get(snapshot.session_id), snapshot, false), position: undefined });
    setView(next); approval.close(); setStatus("Ready");
  }
  async function submit(value: string) {
    if (sending.current) return;
    const failure = queuedMessageFailure(value, sessionId);
    if (failure) throw new Error(failure);
    sending.current = true;
    const root = sessionId;
    const revision = draftRevision.current;
    const message = queue.current.prepare(root, value);
    setView(cache.current.enqueue(message)); setStatus("Queueing message");
    try {
      await queueMessage(token, message);
      queue.current.confirmed(message);
      // HTTP acknowledgements may arrive after typing, navigation, or WS events.
      if (selected.current === root) {
        if (latestPrompt.current === value && draftRevision.current === revision) setPrompt("");
        setStatus(cache.current.get(root).activeRun ? "Working" : "Message queued");
      }
    } catch {
      setStatus("Message acknowledgement unconfirmed");
      throw new Error("Message acknowledgement was not confirmed. Your draft is kept. Retry the same prompt to check its queued identity without starting a duplicate run.");
    } finally { sending.current = false; }
  }
  async function cancel(id: string) {
    if (!id || cancelling.current) return;
    cancelling.current = true; setStopping(true); setStatus("Cancelling and waiting for tools to stop");
    try { await request("cancel", token, { run_id: id }); }
    finally { cancelling.current = false; setStopping(false); }
  }
  return {
    entries: view.entries, prompt, setPrompt, insertDictation, runId: view.activeRun?.id || "", connected,
    backgroundRunId: "", activityError, pendingWakeups: view.entries.filter((entry) => entry.kind === "wakeup" && entry.state === "Scheduled").length,
    transcriptVersion: 0, status: stopping ? "Cancelling and waiting for tools to stop" : status,
    setStatus, approval, loadHistory, submit, cancel,
    pause: () => { stopSubscription.current?.(); setSuspended(true); setConnected(false); },
    reconnect: () => { setSuspended(false); setConnectionVersion((version) => version + 1); },
  };
}
