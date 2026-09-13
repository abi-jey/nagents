import { request } from "./client.js";
import { object, validSessionList } from "./subscription.js";
import type { Session } from "../types.js";

export type TrashItem = { id: string; title: string; deleted_at: number; purge_at: number; deletion_id: string };
export type TrashSnapshot = { revision: string; retention_days: number; items: TrashItem[] };
export type RestoreReply = { restored_session_id: string; sessions: Session[] };
const root = (id: unknown): id is string => typeof id === "string" && /^ngn-[A-Za-z0-9-]{1,76}$/.test(id);
export function validTrashItem(value: unknown): value is TrashItem {
  return object(value) && root(value.id) && typeof value.title === "string" &&
    typeof value.deletion_id === "string" && !!value.deletion_id &&
    typeof value.deleted_at === "number" && Number.isFinite(value.deleted_at) && value.deleted_at >= 0 &&
    typeof value.purge_at === "number" && Number.isFinite(value.purge_at) && value.purge_at > value.deleted_at;
}
export function retentionDays(value: string): number {
  if (!/^\d+$/.test(value.trim()) || !Number.isSafeInteger(Number(value)) || Number(value) < 1 || Number(value) > 365)
    throw new Error("Keep deleted sessions for a whole number of days from 1 to 365.");
  return Number(value);
}
function snapshot(value: unknown): TrashSnapshot {
  if (!object(value) || typeof value.revision !== "string" || !value.revision ||
      !Number.isInteger(value.retention_days) || Number(value.retention_days) < 1 || Number(value.retention_days) > 365 ||
      !Array.isArray(value.items) || !value.items.every(validTrashItem) ||
      new Set(value.items.map((item) => item.id)).size !== value.items.length) throw new Error("Invalid Trash response. Refresh to try again.");
  return value as TrashSnapshot;
}
export async function readTrash(token: string, signal: AbortSignal): Promise<TrashSnapshot> {
  const value: unknown = await (await request("trash", token, undefined, signal)).json();
  signal.throwIfAborted();
  return snapshot(value);
}
export async function saveRetention(token: string, revision: string, days: string): Promise<TrashSnapshot> {
  if (!revision) throw new Error("Refresh Trash before saving its policy.");
  return snapshot(await (await request("trash/settings", token, { revision, retention_days: retentionDays(days) }, undefined, "PUT")).json());
}
function identity(item: TrashItem) {
  if (!validTrashItem(item)) throw new Error("Refresh Trash before changing this session.");
  return { deletion_id: item.deletion_id };
}
export async function restoreTrash(token: string, item: TrashItem): Promise<RestoreReply> {
  const reply: unknown = await (await request(`trash/${encodeURIComponent(item.id)}/restore`, token, identity(item))).json();
  if (!object(reply) || reply.restored_session_id !== item.id || !validSessionList(reply.sessions) ||
      !reply.sessions.some((session) => session.id === item.id)) throw new Error("Restore acknowledgement was not confirmed. Refresh Trash before trying again.");
  return reply as RestoreReply;
}
export async function purgeTrash(token: string, item: TrashItem): Promise<void> {
  const reply: unknown = await (await request(`trash/${encodeURIComponent(item.id)}`, token, identity(item), undefined, "DELETE")).json();
  if (!object(reply) || reply.purged_session_id !== item.id)
    throw new Error("Permanent deletion acknowledgement was not confirmed. Refresh Trash before trying again.");
}
