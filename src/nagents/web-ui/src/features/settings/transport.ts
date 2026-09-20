import { RequestError, request } from "../../api/client.js";
import type { SettingsReply, SettingsValues } from "./types.js";

export type SettingsScope = "workspace" | "global";
const settingsPath = (scope: SettingsScope) => scope === "global" ? "settings/global" : "settings";

export async function readSettings(
  token: string,
  signal: AbortSignal,
  scope: SettingsScope = "workspace",
): Promise<SettingsReply> {
  return (await (
    await request(settingsPath(scope), token, undefined, signal)
  ).json()) as SettingsReply;
}

export async function saveSettings(
  token: string,
  revision: string,
  values: SettingsValues,
  apiKey = "",
  clearApiKey = false,
  scope: SettingsScope = "workspace",
): Promise<SettingsReply> {
  return (await (
    await request(settingsPath(scope), token, {
      revision,
      values,
      api_key: apiKey,
      clear_api_key: clearApiKey,
    })
  ).json()) as SettingsReply;
}

export async function resetSettings(
  token: string,
  revision: string,
  scope: SettingsScope = "workspace",
): Promise<SettingsReply> {
  return (await (
    await request(`${settingsPath(scope)}/reset`, token, { revision })
  ).json()) as SettingsReply;
}

export function settingsFailure(cause: unknown, writing: boolean) {
  const message =
    cause instanceof Error ? cause.message : "Settings request failed.";
  if (cause instanceof RequestError && cause.status === 409) {
    return {
      message: `${message} Your draft is kept. Wait for any active run to finish, then refresh settings before trying again. No request was retried.`,
      needsRefresh: true,
    };
  }
  // A lost write response does not tell us whether the server committed it.
  const uncertain =
    writing && (!(cause instanceof RequestError) || cause.status >= 500);
  return {
    message: uncertain
      ? `${message} The save outcome is unknown. Your draft is kept. Refresh settings to check the server before another change. No request was retried.`
      : message,
    needsRefresh: uncertain,
  };
}
