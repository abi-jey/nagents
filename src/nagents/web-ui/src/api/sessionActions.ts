import { request } from "./client.js";
import { validSnapshot } from "./subscription.js";
import type { Snapshot } from "../types.js";

export function sessionTitle(value: string, required: boolean): string {
  const title = value.trim();
  if ((required && !title) || [...title].length > 80 || /[\p{C}\p{Z}]/u.test(title.replaceAll(" ", "")))
    throw new Error("Choose a chat title of 1–80 characters without control characters.");
  return title;
}

export async function changeSession(token: string, id: string, action: "fork" | "rename", value: string): Promise<Snapshot> {
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(id)) throw new Error("Invalid chat. Refresh before trying again.");
  const title = sessionTitle(value, action === "rename");
  const result: unknown = await (await request(`sessions/${encodeURIComponent(id)}/${action}`, token, { title })).json();
  if (!validSnapshot(result) || (action === "fork" && result.session_id === id))
    throw new Error("Invalid chat response. Reconnect to check the result before trying again.");
  return result;
}
