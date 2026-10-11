import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "../../api/client.js";

export type SkillItem = { name: string; description: string; command: string; canonical_command: string; enabled: boolean };
export type SkillsCatalog = { session_id: string; agent: string; enabled: boolean; skills: SkillItem[] };

export function useSkills(token: string, sessionId: string, agent: string, prompt: string, runId: string) {
  const scope = JSON.stringify([token, sessionId, agent]);
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [state, setState] = useState<{ scope: string; catalog?: SkillsCatalog; loading: boolean; error: string }>({ scope: "", loading: false, error: "" });
  const controller = useRef<AbortController | undefined>(undefined);
  const watching = open || /^\/[A-Za-z0-9:_-]*$/.test(prompt);
  const refresh = useCallback(async () => {
    controller.current?.abort();
    if (!token || !sessionId) { setState({ scope, loading: false, error: "Reconnect to browse skills." }); return; }
    const pending = new AbortController(); controller.current = pending;
    setState(previous => ({ scope, catalog: previous.scope === scope ? previous.catalog : undefined, loading: true, error: "" }));
    try {
      const catalog = await (await request(`skills?session_id=${encodeURIComponent(sessionId)}`, token, undefined, pending.signal)).json() as SkillsCatalog;
      // A designed session can resolve a different agent than the global UI preference.
      if (catalog.session_id !== sessionId || typeof catalog.agent !== "string" || !Array.isArray(catalog.skills)) throw new Error("Skill catalog changed.");
      if (!pending.signal.aborted) setState({ scope, catalog, loading: false, error: "" });
    } catch {
      if (!pending.signal.aborted) setState({ scope, loading: false, error: "Could not load skills. Refresh to try again." });
    }
  }, [token, sessionId, agent, scope]);
  useEffect(() => {
    void refresh();
    const focused = () => { if (document.visibilityState !== "hidden") void refresh(); };
    window.addEventListener("focus", focused);
    const interval = watching ? window.setInterval(focused, 10000) : undefined;
    return () => { window.removeEventListener("focus", focused); window.clearInterval(interval); controller.current?.abort(); };
  }, [refresh, watching, runId]);
  const current = state.scope === scope ? state : { loading: !!token, error: "" };
  const catalog = "catalog" in current ? current.catalog : undefined;
  return { open, search, setSearch, show: (query = "") => { setSearch(query); setOpen(true); }, close: () => setOpen(false),
    catalog, items: catalog?.enabled ? catalog.skills.filter(skill => skill.enabled) : [], loading: current.loading, error: current.error, refresh };
}

export type SkillsState = ReturnType<typeof useSkills>;
