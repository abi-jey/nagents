import { useRef, useState } from "react";

/** One synchronous admission gate for browser mutations, with shared feedback. */
export function useOperations() {
  const occupied = useRef(false);
  const [operating, setOperating] = useState(false);
  const [error, setError] = useState("");

  async function run(action: () => Promise<void>) {
    if (occupied.current) throw new Error("Finish the current operation first. Your draft is kept.");
    occupied.current = true;
    setOperating(true);
    setError("");
    try { await action(); }
    finally { occupied.current = false; setOperating(false); }
  }

  async function operate(action: () => Promise<void>): Promise<boolean> {
    if (occupied.current) return false;
    try { await run(action); return true; }
    catch (cause) {
      setError(cause instanceof Error ? cause.message : "Local request failed. No operation was retried.");
      return false;
    }
  }

  return { operating, error, setError, occupied: () => occupied.current, operate, run };
}
