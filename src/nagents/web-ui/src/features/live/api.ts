import { request } from "../../api/client.js";
import type { LiveCapability, LiveCreated, LiveSnapshot } from "./types.js";

export async function capability(token: string, signal?: AbortSignal): Promise<LiveCapability> {
  return (await request("live", token, undefined, signal)).json() as Promise<LiveCapability>;
}

export function liveApi(token: string) {
  return {
    create: async (sdp: string, voice: string, signal: AbortSignal, revision: string): Promise<LiveCreated> =>
      (await request("live/sessions", token, { sdp, voice, revision }, signal)).json() as Promise<LiveCreated>,
    read: async (id: string, after: number, signal: AbortSignal): Promise<LiveSnapshot> =>
      (await request(`live/sessions/${encodeURIComponent(id)}?after=${after}`, token, undefined, signal)).json() as Promise<LiveSnapshot>,
    close: async (id: string): Promise<LiveSnapshot> =>
      (await request(`live/sessions/${encodeURIComponent(id)}/close`, token, {}, AbortSignal.timeout(45_000))).json() as Promise<LiveSnapshot>,
  };
}
