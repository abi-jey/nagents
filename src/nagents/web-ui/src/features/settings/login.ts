import { request } from "../../api/client.js";

export type LoginStatus = "idle" | "starting" | "pending" | "completed" | "failed" | "cancelled";
export type LoginReply = {
  id: string; status: LoginStatus; verification_url: string; user_code: string;
  expires_in: number; message: string; provider: string;
};
export const deviceLoginUrl = "https://auth.openai.com/codex/device";
const statuses: readonly string[] = ["idle", "starting", "pending", "completed", "failed", "cancelled"];
export function parseLogin(value: unknown): LoginReply {
  if (!value || typeof value !== "object") throw new Error("Invalid login status. Reload the sign-in dialog.");
  const data = value as Record<string, unknown>;
  if (typeof data.id !== "string" || typeof data.status !== "string" || !statuses.includes(data.status) ||
      typeof data.message !== "string" || typeof data.provider !== "string" ||
      typeof data.expires_in !== "number" || !Number.isFinite(data.expires_in) || data.expires_in < 0 ||
      typeof data.user_code !== "string" || typeof data.verification_url !== "string" ||
      (data.verification_url && data.verification_url !== deviceLoginUrl))
    throw new Error("Invalid login status. Reload the sign-in dialog.");
  const pending = data.status === "pending";
  return { id: data.id, status: data.status as LoginStatus, message: data.message, provider: data.provider,
    expires_in: data.expires_in, user_code: pending ? data.user_code : "", verification_url: pending ? data.verification_url : "" };
}
export async function loginRequest(token: string, action: "read" | "start" | "cancel", id = "", signal?: AbortSignal) {
  const response = await request(`login/chatgpt${action === "cancel" ? "/cancel" : ""}`, token,
    action === "read" ? undefined : action === "cancel" ? { id } : {}, signal);
  return parseLogin(await response.json());
}
