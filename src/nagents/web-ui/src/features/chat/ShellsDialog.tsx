import { useEffect, useRef, useState } from "react";
import { request } from "../../api/client.js";

type Job = { shell_id: string; state: string; command: string; output?: string; reason?: string; output_path?: string };
const active = (shell: Job) => shell.state === "starting" || shell.state === "running";

export function ShellsDialog({ token, sessionId, close }: { token: string; sessionId: string; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [shells, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState("");
  const path = `sessions/${encodeURIComponent(sessionId)}/shells`;
  useEffect(() => {
    const previous = document.activeElement;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); if (previous instanceof HTMLElement) previous.focus(); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const response = await request(path, token, undefined, controller.signal);
        const data = await response.json() as { shells: Job[] };
        if (!controller.signal.aborted) { setJobs(data.shells); setError(""); }
      } catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Unable to load shells."); }
      if (!controller.signal.aborted) timer = setTimeout(() => void refresh(), 1000);
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, token]);
  async function stop(shell: Job) {
    try {
      const updated = await (await request(`${path}/${encodeURIComponent(shell.shell_id)}/stop`, token, {})).json() as Job;
      setJobs(current => current.map(item => item.shell_id === updated.shell_id ? updated : item));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to stop shell."); }
  }
  async function download(shell: Job) {
    try {
      const response = await request(`${path}/${encodeURIComponent(shell.shell_id)}/output`, token);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a"); link.href = url; link.download = `shell-${shell.shell_id}.log`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to download shell output."); }
  }
  return <dialog className="workspace-dialog command-help" ref={dialog} aria-labelledby="shells-title"
    onCancel={event => { event.preventDefault(); close(); }}>
    <header><h2 id="shells-title">Shells</h2><button type="button" onClick={close} aria-label="Close shells">Close</button></header>
    <p>Jobs belong to this conversation. Process output is untrusted data. Download captures the log available now.</p>
    {error && <p role="alert">{error}</p>}
    {!shells.length && <p>No managed shells in this conversation.</p>}
    {shells.map(shell => <section key={shell.shell_id}>
      <h3><code>{shell.shell_id}</code> / {shell.state}</h3><pre>{shell.command}</pre>
      {shell.reason && <p>{shell.reason}</p>}{shell.output_path && <p><code>{shell.output_path}</code></p>}
      {shell.output && <pre style={{ maxHeight: "18rem", overflow: "auto", whiteSpace: "pre-wrap" }}>{shell.output}</pre>}
      {active(shell) && <button type="button" onClick={() => void stop(shell)}>Stop shell</button>}
      <button type="button" onClick={() => void download(shell)}>Download output</button>
    </section>)}
  </dialog>;
}
