import { useCallback, useEffect, useState } from "react";
import { readContextStats } from "../../api/context";
import type { ContextStats } from "../../types";

export function useContextStats(token: string, sessionId: string, idle: boolean) {
  const [stats, setStats] = useState<ContextStats>();
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!token || !sessionId || !idle) return;
    const controller = new AbortController();
    setError("");
    readContextStats(token, sessionId, controller.signal)
      .then(setStats)
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : "Context statistics unavailable.");
      });
    return () => controller.abort();
  }, [token, sessionId, idle, revision]);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  return { stats, error, open, setOpen, refresh };
}
