import { request, RequestError } from "./client.js";
import { validSnapshot } from "./subscription.js";
import type { Snapshot } from "../types.js";

const retryDelays = [100, 250, 500, 1000] as const;

function waitForRetry(delay: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const abort = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, delay);
    signal.addEventListener("abort", abort, { once: true });
  });
}

async function read(path: string, token: string, signal: AbortSignal): Promise<Snapshot> {
  const response = await request(path, token, undefined, signal);
  const value: unknown = await response.json();
  signal.throwIfAborted();
  if (!validSnapshot(value)) throw new Error("Invalid session history. Reconnect to try again.");
  return value;
}

/** A busy startup read can race admission/cleanup; never replay a mutation. */
export async function readSessionHistory(token: string, signal: AbortSignal, fallbackRoot = "", pause = waitForRetry): Promise<Snapshot> {
  for (let attempt = 0; ; attempt++) {
    signal.throwIfAborted();
    try {
      try { return await read("sessions", token, signal); }
      catch (cause) {
        signal.throwIfAborted();
        // A known root can be read without moving the executing Harness. Keep
        // this fast recovery path for existing/background runs on older hosts.
        if (!(cause instanceof RequestError) || cause.status !== 409 || !fallbackRoot) throw cause;
        return await read(`sessions/${encodeURIComponent(fallbackRoot)}`, token, signal);
      }
    } catch (cause) {
      signal.throwIfAborted();
      if (!(cause instanceof RequestError) || cause.status !== 409 || attempt >= retryDelays.length) throw cause;
      await pause(retryDelays[attempt], signal);
    }
  }
}
