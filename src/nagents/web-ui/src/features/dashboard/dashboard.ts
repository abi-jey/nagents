import { request } from "../../api/client.js";

export type DashboardSnapshot = { workspace: string; status: "not_connected" };

export async function readDashboard(signal: AbortSignal): Promise<DashboardSnapshot> {
  // The standalone dashboard host has no chat session; its bootstrap is minimal.
  const bootstrap: unknown = await (await request("bootstrap", "", undefined, signal)).json();
  signal.throwIfAborted();
  if (!bootstrap || typeof bootstrap !== "object" || !("token" in bootstrap) ||
      typeof bootstrap.token !== "string" || !/^[A-Za-z0-9_-]{1,256}$/.test(bootstrap.token))
    throw new Error("Dashboard connection is unavailable.");
  const response = await request("dashboard", bootstrap.token, undefined, signal);
  const value: unknown = await response.json();
  signal.throwIfAborted();
  if (!value || typeof value !== "object" || !("workspace" in value) ||
      typeof value.workspace !== "string" || !("status" in value) || value.status !== "not_connected")
    throw new Error("Dashboard metadata is unavailable.");
  return { workspace: value.workspace, status: value.status };
}

/** Call directly from the command gesture, before any asynchronous work. */
export function openDashboard(): void {
  window.open("/dashboard", "_blank", "noopener,noreferrer");
}

export function isDashboardPath(pathname: string): boolean {
  return pathname === "/dashboard" || pathname === "/dashboard/";
}
