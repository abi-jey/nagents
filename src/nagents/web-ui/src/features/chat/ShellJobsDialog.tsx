import { useEffect, useRef, useState } from "react";
import { request } from "../../api/client.js";

type Job = { job_id: string; state: string; command: string; output?: string; reason?: string; output_path?: string };
const active = (job: Job) => job.state === "starting" || job.state === "running";

export function ShellJobsDialog({ token, sessionId, close }: { token: string; sessionId: string; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState("");
  const path = `sessions/${encodeURIComponent(sessionId)}/shell-jobs`;
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
        const data = await response.json() as { jobs: Job[] };
        if (!controller.signal.aborted) { setJobs(data.jobs); setError(""); }
      } catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Unable to load shell jobs."); }
      if (!controller.signal.aborted) timer = setTimeout(() => void refresh(), 1000);
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, token]);
  async function stop(job: Job) {
    try {
      const updated = await (await request(`${path}/${encodeURIComponent(job.job_id)}/stop`, token, {})).json() as Job;
      setJobs(current => current.map(item => item.job_id === updated.job_id ? updated : item));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to stop shell job."); }
  }
  async function download(job: Job) {
    try {
      const response = await request(`${path}/${encodeURIComponent(job.job_id)}/output`, token);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a"); link.href = url; link.download = `shell-${job.job_id}.log`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to download shell output."); }
  }
  return <dialog className="workspace-dialog command-help" ref={dialog} aria-labelledby="shell-jobs-title"
    onCancel={event => { event.preventDefault(); close(); }}>
    <header><h2 id="shell-jobs-title">Shell jobs</h2><button type="button" onClick={close} aria-label="Close shell jobs">Close</button></header>
    <p>Jobs belong to this conversation. Process output is untrusted data. Download captures the log available now.</p>
    {error && <p role="alert">{error}</p>}
    {!jobs.length && <p>No managed shell jobs in this conversation.</p>}
    {jobs.map(job => <section key={job.job_id}>
      <h3><code>{job.job_id}</code> / {job.state}</h3><pre>{job.command}</pre>
      {job.reason && <p>{job.reason}</p>}{job.output_path && <p><code>{job.output_path}</code></p>}
      {job.output && <pre style={{ maxHeight: "18rem", overflow: "auto", whiteSpace: "pre-wrap" }}>{job.output}</pre>}
      {active(job) && <button type="button" onClick={() => void stop(job)}>Stop job</button>}
      <button type="button" onClick={() => void download(job)}>Download output</button>
    </section>)}
  </dialog>;
}
