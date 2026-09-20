import { useCallback, useEffect, useRef, useState } from "react";
import { readContextStats } from "../../api/context";
import type { ContextStats } from "../../types";
import { ContextRefresh } from "./controller";

export function useContextStats(token: string, sessionId: string, options: {
  active: boolean; revision: number; configuration: string; enabled: boolean;
}) {
  const key = `${token}:${sessionId}`;
  const [result, setResult] = useState<{ key: string; stats: ContextStats }>();
  const [failure, setFailure] = useState({ key: "", message: "" });
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const controller = useRef<ContextRefresh | undefined>(undefined);
  useEffect(() => {
    setLoading(false);
    if (!token || !sessionId || !options.enabled) return;
    const refresh = new ContextRefresh({
      read: (signal) => readContextStats(token, sessionId, signal),
      accept: (stats) => setResult({ key, stats }),
      error: (message) => setFailure({ key, message }), loading: setLoading,
    });
    controller.current = refresh;
    refresh.refresh(true);
    return () => { refresh.dispose(); controller.current = undefined; };
  }, [token, sessionId, options.enabled]);
  useEffect(() => { controller.current?.refresh(); }, [options.revision, options.configuration, options.active]);
  useEffect(() => {
    if (!options.active || !options.enabled) return;
    const timer = setInterval(() => controller.current?.refresh(), 1000);
    return () => clearInterval(timer);
  }, [options.active, options.enabled, token, sessionId]);
  const refresh = useCallback(() => controller.current?.refresh(true), []);
  return { stats: result?.key === key ? result.stats : undefined, error: failure.key === key ? failure.message : "", loading, live: options.active, open, setOpen, refresh };
}
