import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { request } from "../../../api/client.js";
import { Icon } from "../../../components/Icon.js";
import { announceSettingsSaved } from "./state.js";

interface Connection {
  url: string; rpc_url: string; issuer: string; client_id: string; scopes: string[];
  resource: string; token_env: string; ca_file: string; loopback_http: boolean; timeout: number; metadata_url: string;
}
interface Remote { name: string; description: string; connection: Connection; signed_in: boolean; login_error?: string; user_invocable: boolean; subagent_available: boolean; }
interface Catalog { revision: string; agents: Remote[]; source_path: string; }
interface Discovery { connection: Connection; name: string; description: string; authentication: string; }
const initial: Connection = { url: "", rpc_url: "", issuer: "", client_id: "", scopes: ["openid"], resource: "", token_env: "", ca_file: "", loopback_http: false, timeout: 120, metadata_url: "" };

export function RemoteAgentEditor({ agent, token, saved, active = true }: { agent: string; token: string; saved: (name: string) => void; active?: boolean }) {
  const [catalog, setCatalog] = useState<Catalog>();
  const [name, setName] = useState(agent);
  const [description, setDescription] = useState("");
  const [connection, setConnection] = useState<Connection>(initial);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [signedIn, setSignedIn] = useState(false);
  const [signInPending, setSignInPending] = useState("");
  const current = catalog?.agents.find(item => item.name === agent);
  const dirty = !!current && (current.connection.url !== connection.url || current.connection.ca_file !== connection.ca_file || current.description !== description);
  const callback = `${window.location.origin}/remote-agent-login/callback`;
  useEffect(() => {
    const abort = new AbortController();
    void request("remote-agents", token, undefined, abort.signal).then(reply => reply.json() as Promise<Catalog>).then(reply => {
      if (abort.signal.aborted) return;
      setCatalog(reply);
      const remote = reply.agents.find(item => item.name === agent);
      setName(agent); setDescription(remote?.description || ""); setConnection(remote?.connection || initial);
      setSignedIn(remote?.signed_in || false); setError(remote?.login_error || "");
    }).catch(cause => { if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not read remote connections."); });
    return () => abort.abort();
  }, [agent, token]);
  useEffect(() => {
    if (!agent || !active) return;
    const abort = new AbortController();
    const check = () => void request("remote-agents", token, undefined, abort.signal).then(reply => reply.json() as Promise<Catalog>).then(reply => {
      if (abort.signal.aborted) return;
      const remote = reply.agents.find(item => item.name === agent);
      setSignedIn(remote?.signed_in || false);
      if (remote?.login_error) setError(remote.login_error);
    }).catch(() => { /* Preserve the form if a status read fails. */ });
    window.addEventListener("focus", check);
    return () => { abort.abort(); window.removeEventListener("focus", check); };
  }, [agent, token, active]);
  useEffect(() => {
    if (signInPending !== agent || !agent || !active) return;
    const abort = new AbortController();
    let reading = false;
    const check = async () => {
      if (reading) return;
      reading = true;
      try {
        const reply = await (await request("remote-agents", token, undefined, abort.signal)).json() as Catalog;
        if (abort.signal.aborted) return;
        const remote = reply.agents.find(item => item.name === agent);
        if (remote?.signed_in) { setSignedIn(true); setSignInPending(""); setMessage("Signed in. You can now use this remote agent."); }
        else if (remote?.login_error) { setError(remote.login_error); setSignInPending(""); }
      } catch { /* The next bounded poll can recover a transient status read. */ }
      finally { reading = false; }
    };
    const interval = window.setInterval(() => void check(), 2500);
    const timeout = window.setTimeout(() => { setSignInPending(""); setError(`Sign-in did not complete. If the provider rejected the callback, register ${callback} in the provider and try again.`); }, 300000);
    return () => { abort.abort(); window.clearInterval(interval); window.clearTimeout(timeout); };
  }, [signInPending, agent, token, active, callback]);
  const update = <K extends keyof Connection>(key: K, value: Connection[K]) => setConnection(current => ({ ...current, [key]: value }));
  async function connect(event: FormEvent) {
    event.preventDefault(); if (!catalog) return;
    const popup = !agent ? window.open("about:blank", "_blank") : null;
    if (popup) popup.opener = null;
    let launched = false;
    setBusy(true); setError(""); setMessage("");
    try {
      const discovered = await (await request("remote-agents/discover", token, { ...initial, url: connection.url, ca_file: connection.ca_file, timeout: connection.timeout })).json() as Discovery;
      if (discovered.authentication === "manual") throw new Error("This host does not advertise browser sign-in. Its administrator needs to publish OAuth/OpenID Connect metadata.");
      const identifier = agent || name || discovered.name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-|-$/g, "").slice(0, 128);
      if (!identifier) throw new Error("Enter a name for this remote agent.");
      const reply = await request(`remote-agents/${encodeURIComponent(identifier)}`, token, { revision: catalog.revision, description: description || discovered.description, connection: discovered.connection, user_invocable: current?.user_invocable ?? true, subagent_available: current?.subagent_available ?? true }, undefined, "PUT");
      const stored = await reply.json() as Catalog;
      setCatalog(stored); setConnection(discovered.connection); setName(identifier);
      setMessage("Connected. Available to the next assistant round without restarting.");
      announceSettingsSaved(); saved(identifier);
      if (!agent && discovered.connection.issuer && discovered.connection.client_id && !stored.agents.find(item => item.name === identifier)?.signed_in) {
        const auth = await (await request(`remote-agents/${encodeURIComponent(identifier)}/login`, token, {})).json() as { authorization_url: string };
        setSignInPending(identifier); launched = true;
        if (popup) popup.location.href = auth.authorization_url;
        else window.location.assign(auth.authorization_url);
      } else popup?.close();
    } catch (cause) { if (!launched) popup?.close(); setError(cause instanceof Error ? cause.message : "Could not connect the remote agent."); }
    finally { setBusy(false); }
  }
  async function login() {
    const popup = window.open("about:blank", "_blank");
    if (popup) popup.opener = null;
    setBusy(true); setError("");
    try {
      const reply = await (await request(`remote-agents/${encodeURIComponent(agent)}/login`, token, {})).json() as { authorization_url: string };
      if (popup) popup.location.href = reply.authorization_url;
      else window.location.assign(reply.authorization_url);
      setSignInPending(agent);
      setMessage("Complete sign-in, then return here. If the provider rejects the callback, register the URL shown below in the provider.");
    } catch (cause) { popup?.close(); setError(cause instanceof Error ? cause.message : "Could not start sign-in."); }
    finally { setBusy(false); }
  }
  async function probe() {
    setBusy(true); setError(""); setMessage("");
    try { const reply = await (await request(`remote-agents/${encodeURIComponent(agent)}/probe`, token, {})).json() as { message: string; name: string }; setMessage(`${reply.message} ${reply.name}`); setSignedIn(true); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not check the connection."); }
    finally { setBusy(false); }
  }
  const hostSetupMissing = !!connection.issuer && !connection.client_id;
  return <div className="an-scroll"><form className="an-remote-editor" onSubmit={event => void connect(event)}>
    <div className="an-section-heading"><Icon name="plug" size={20} /><div><h3>{agent ? "Remote connection" : "Connect a remote agent"}</h3><p>Enter its address. ngn discovers the agent and sign-in requirements from the host.</p></div></div>
    <label>Server URL<input type="url" required value={connection.url} onChange={event => update("url", event.target.value)} placeholder="https://agent.example.test:8443" /></label>
    {agent && <div className="an-remote-login"><span>{hostSetupMissing ? "Host sign-in setup required" : connection.issuer ? signedIn ? "Signed in" : "Sign-in required" : "Connected"}</span>{connection.issuer && <button type="button" className="primary" disabled={busy || dirty || hostSetupMissing} onClick={() => void login()}>{signedIn ? "Sign in again" : "Sign in"}</button>}</div>}
    {hostSetupMissing && <p role="alert">The remote host has not advertised a public sign-in client. Its administrator must configure the client and register this callback URL in the identity provider. You do not need to enter issuer, client ID or scopes here.</p>}
    {(connection.issuer || hostSetupMissing) && <div className="an-remote-authorization"><p>Provider callback URL</p><code>{callback}</code><p>If the provider refuses sign-in or reports an invalid redirect, this exact URL must be registered for the host’s public client in the provider.</p></div>}
    <details><summary>Connection details</summary>{!agent && <label>Agent name · optional<input pattern={"[A-Za-z0-9_\\-]{1,128}"} value={name} onChange={event => setName(event.target.value)} placeholder="Use the remote agent’s name" autoComplete="off" /></label>}<label>Description · optional<input value={description} onChange={event => setDescription(event.target.value)} placeholder="Use the remote agent’s description" /></label><label>Additional CA bundle on ngn server<input value={connection.ca_file} onChange={event => update("ca_file", event.target.value)} placeholder="Optional path to a trusted CA file" /></label><label>Request timeout · seconds<input type="number" min="1" max="3600" value={connection.timeout} onChange={event => update("timeout", Number(event.target.value))} /></label>{connection.issuer && <dl><dt>Sign-in service · advertised by host</dt><dd>{connection.issuer}</dd><dt>Public client</dt><dd>{connection.client_id || "Not advertised"}</dd><dt>Requested scopes</dt><dd>{connection.scopes.join(" ")}</dd></dl>}</details>
    {error && <p role="alert" className="settings-error">{error}</p>}{message && <p role="status">{message}</p>}
    <footer><button type="submit" disabled={busy || !catalog}>{busy ? "Working…" : agent ? "Refresh connection" : "Connect"}</button>{agent && <button type="button" disabled={busy || dirty} onClick={() => void probe()}>Check connection</button>}<span>JSON · reloads each round</span></footer>
    {catalog && <p className="an-source-path">{catalog.source_path}</p>}
  </form></div>;
}
