import { request } from "./client.js";

export type ChatFolder = { id: string; name: string; collapsed: boolean; parent_id: string };
export type ChatFolders = { revision: string; groups: ChatFolder[]; memberships: Record<string, string> };
const groupId = /^group-[a-f0-9]{32}$/;
const rootId = /^[A-Za-z0-9_-]{1,128}$/;
const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

export function parseFolders(value: unknown): ChatFolders {
  const invalid = () => new Error("Invalid chat folders. Refresh to try again.");
  if (!record(value) || typeof value.revision !== "string" || !/^[a-f0-9]{32}$/.test(value.revision)
      || !Array.isArray(value.groups) || value.groups.length > 128 || !record(value.memberships)) throw invalid();
  const groups: ChatFolder[] = [];
  const ids = new Set<string>();
  for (const item of value.groups) {
    if (!record(item) || typeof item.id !== "string" || !groupId.test(item.id) || ids.has(item.id)
        || typeof item.name !== "string" || !item.name.trim() || [...item.name].length > 80
        || typeof item.collapsed !== "boolean" || (item.parent_id !== undefined && typeof item.parent_id !== "string")) throw invalid();
    ids.add(item.id);
    groups.push({ id: item.id, name: item.name, collapsed: item.collapsed, parent_id: typeof item.parent_id === "string" ? item.parent_id : "" });
  }
  const byId = new Map(groups.map(group => [group.id, group]));
  for (const group of groups) {
    const visited = new Set([group.id]);
    let parent = group.parent_id;
    while (parent) {
      if (!ids.has(parent) || visited.has(parent) || visited.size >= 12) throw invalid();
      visited.add(parent); parent = byId.get(parent)!.parent_id;
    }
  }
  const memberships: Record<string, string> = Object.create(null) as Record<string, string>;
  for (const [id, folder] of Object.entries(value.memberships)) {
    if (!rootId.test(id) || typeof folder !== "string" || !ids.has(folder)) throw invalid();
    memberships[id] = folder;
  }
  return { revision: value.revision, groups, memberships };
}

export async function readFolders(token: string, signal?: AbortSignal) {
  return parseFolders(await (await request("session-groups", token, undefined, signal)).json());
}
export async function changeFolder(token: string, action: "create" | "update" | "move" | "remove" | "reparent", body: object) {
  return parseFolders(await (await request(`session-groups/${action}`, token, body)).json());
}
