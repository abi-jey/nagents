import { useEffect, useRef, useState } from "react";
import { deviceLoginUrl, loginRequest, type LoginReply } from "./login.js";

export function LoginDialog({ token, close, applied }: {
  token: string; close: () => void; applied: () => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const current = useRef<LoginReply>(undefined);
  const [reply, setReply] = useState<LoginReply>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const writing = useRef(false);
  const mounted = useRef(false);
  const revision = useRef(0);
  const uncertain = useRef(false);
  const appliedId = useRef("");
  const read = useRef<AbortController>(undefined);
  const active = reply?.status === "starting" || reply?.status === "pending";
  async function receive(value: LoginReply) {
    if (!mounted.current) return;
    current.current = value; uncertain.current = false; setReply(value); setError("");
    if (value.status === "completed" && appliedId.current !== value.id) {
      await applied();
      appliedId.current = value.id;
    }
  }
  async function refresh() {
    if (writing.current || read.current) return;
    const controller = new AbortController(), version = revision.current;
    read.current = controller;
    try {
      const value = await loginRequest(token, "read", "", controller.signal);
      if (!controller.signal.aborted && version === revision.current) await receive(value);
    } catch (cause) {
      if (!controller.signal.aborted && mounted.current) setError(cause instanceof Error ? cause.message : "Could not read login status.");
    } finally { if (read.current === controller) read.current = undefined; }
  }
  useEffect(() => {
    mounted.current = true;
    const previous = document.activeElement;
    dialog.current?.showModal();
    void refresh();
    const timer = setInterval(() => { if (uncertain.current || !current.current || ["starting", "pending"].includes(current.current.status)) void refresh(); }, 1000);
    return () => {
      mounted.current = false; clearInterval(timer); read.current?.abort(); read.current = undefined;
      dialog.current?.close(); if (previous instanceof HTMLElement) previous.focus();
    };
  }, [token]);

  async function action(kind: "start" | "cancel", dismiss = false) {
    if (writing.current) return;
    writing.current = true; setBusy(true); setError(""); setCopied(false);
    revision.current++; read.current?.abort(); read.current = undefined;
    try {
      const next = await loginRequest(token, kind, current.current?.id || "");
      await receive(next);
      if (dismiss && next.status !== "pending" && next.status !== "starting") close();
    } catch (cause) {
      uncertain.current = true;
      try { await receive(await loginRequest(token, "read")); }
      catch { /* Keep reconciling through the regular status poll. */ }
      if (mounted.current) setError(cause instanceof Error ? cause.message : "Sign-in request failed. Reload status before retrying.");
    } finally { writing.current = false; if (mounted.current) setBusy(false); }
  }
  function dismiss() {
    if (busy) return;
    if (uncertain.current) { void refresh(); return; }
    if (active) void action("cancel", true);
    else close();
  }
  return <dialog ref={dialog} className="workspace-dialog login-dialog" aria-labelledby="login-title"
    onCancel={event => { event.preventDefault(); dismiss(); }}>
    <header><h2 id="login-title">ChatGPT / Codex sign-in</h2><button type="button" disabled={busy} onClick={dismiss} aria-label="Close sign-in">×</button></header>
    <p>Sign in to the ngn server with your ChatGPT account. Credentials stay on the server and never enter the conversation.</p>
    {reply?.status === "pending" && <div className="device-login-code">
      <p>Open the sign-in page and enter this code:</p>
      <output aria-label="Device sign-in code">{reply.user_code}</output>
      <p>Expires in {Math.max(1, Math.ceil(reply.expires_in / 60))} minute{reply.expires_in > 60 ? "s" : ""}.</p>
      <a href={deviceLoginUrl} target="_blank" rel="noopener noreferrer">Open OpenAI sign-in ↗</a>
      <button type="button" onClick={() => {
        if (!navigator.clipboard) { setError("Copy unavailable. Select the code above."); return; }
        void navigator.clipboard.writeText(reply.user_code).then(() => setCopied(true)).catch(() => setError("Copy unavailable. Select the code above."));
      }}>{copied ? "Copied" : "Copy code"}</button>
    </div>}
    <p role="status">{reply?.message || (reply ? "Start sign-in to request a one-time code." : "Checking sign-in status…")}</p>
    {reply?.status === "completed" && <p>Your saved login is ready. Model discovery and new conversations use the updated connection.</p>}
    {error && <p role="alert" className="error-text">{error}</p>}
    <footer>
      {error && <button type="button" disabled={busy} onClick={() => void refresh()}>Reload status</button>}
      <button type="button" disabled={busy} onClick={dismiss}>{active ? "Cancel sign-in" : "Close"}</button>
      {!active && <button type="button" className="primary" disabled={busy || !reply}
        onClick={() => void action("start")}>{busy ? "Starting…" : reply?.status === "completed" ? "Sign in again" : "Sign in with ChatGPT"}</button>}
    </footer>
  </dialog>;
}
