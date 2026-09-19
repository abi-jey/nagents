import { useEffect, useState } from "react";
import { request } from "../../api/client";
import type { Design } from "./types";

interface Catalog {
  connections: { id: string; plugin: string; enabled: boolean; status: string }[];
  routes: { connection: string; design: string; agent: string }[];
}

export function ChannelRoutes({ token, design, change, configure, busy, apply }: {
  token: string; design: Design; change: (design: Design) => void; configure: () => void;
  busy: boolean; apply: () => Promise<void>;
}) {
  const [catalog, setCatalog] = useState<Catalog>();
  const [error, setError] = useState("");
  async function refresh() {
    try { setCatalog(await (await request("designer/channels", token)).json() as Catalog); setError(""); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not load channel connections."); }
  }
  useEffect(() => { void refresh(); }, [token]);
  const ids = [...new Set([...(catalog?.connections.map((connection) => connection.id) || []), ...Object.keys(design.channels || {})])];
  return <section aria-label="Designer channels">
    <h2>Channels</h2>
    <p>Route a connection to a designed agent. Channel listeners run only in ngn serve.</p>
    <div className="designer-tabs"><button onClick={configure}>Configure connections</button><button onClick={() => void refresh()}>Refresh connections</button></div>
    {error && <p role="alert">{error}</p>}
    {!ids.length && <p>No channel connections yet. Configure Telegram or another installed connector, then refresh.</p>}
    {ids.map((id) => {
      const connection = catalog?.connections.find((item) => item.id === id);
      const active = catalog?.routes.find((item) => item.connection === id);
      return <fieldset key={id}><legend>{id} · {connection?.plugin || "Not configured"}</legend>
        <p>{connection?.status || "unavailable"}{active ? ` · Active route: ${active.design} / ${active.agent}` : " · Default web agent"}</p>
        <label>Agent for {id}<select value={design.channels?.[id] || ""} onChange={(event) => {
          const channels = { ...design.channels };
          if (event.target.value) channels[id] = event.target.value; else delete channels[id];
          change({ ...design, channels });
        }}><option value="">Not routed by this design</option>{Object.entries(design.agents).map(([agent, value]) => <option key={agent} value={agent}>{value.name || agent} ({agent})</option>)}</select></label>
      </fieldset>;
    })}
    <p>Credentials and connector settings stay in the existing channel store. YAML contains only connection IDs and agent bindings.</p>
    <p>Apply routes to publish a saved snapshot for new conversations. Existing chats keep their current agent and history; use /new in Telegram to start with the new route.</p>
    <button disabled={busy || !catalog} onClick={() => void apply().then(refresh)}>Save & apply channel routes</button>
  </section>;
}
