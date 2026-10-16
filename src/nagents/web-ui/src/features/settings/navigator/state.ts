import { useEffect, useRef, useState } from "react";
import { request, RequestError } from "../../../api/client.js";
import { settingsFailure } from "../transport.js";
import type { ModelBinding } from "../types.js";
import type { LiveSettingsSnapshot, VoiceContextMode } from "../../live/types.js";

export type NavigatorScope = "global" | "workspace" | "agent";
export type NavigatorSection = "Overview" | "Model" | "Instructions" | "Tools" | "Voice";
export interface NavigatorTarget { scope: NavigatorScope; agent: string; }
export interface NavigatorRequest extends NavigatorTarget { section: NavigatorSection; serial: number; }
export const targetKey = (target: NavigatorTarget) => target.scope === "agent" ? `agent:${target.agent}` : target.scope;
export const targetQuery = (target: NavigatorTarget) => `scope=${target.scope}${target.scope === "agent" ? `&agent=${encodeURIComponent(target.agent)}` : ""}`;
export const scopeName = (target: NavigatorTarget) => target.scope === "agent" ? target.agent : target.scope === "global" ? "Global defaults" : "Workspace";
export const sourceLabel = (source: string) => ({ agent: "Agent override", definition: "Agent definition", workspace: "Workspace", global: "Global", builtin: "Built-in default", startup: "Startup default" })[source] || source;
export interface AgentPreferences { instructions?: string; tools?: string[]; }
export interface AgentProfile { id: string; name: string; description: string; source: string; mode: string; source_format?: string; }
export interface AgentSettingsReply {
  scope: NavigatorScope; agent: string; revision: string; preferences: AgentPreferences;
  values: { instructions: string; tools: string[] }; origins: { instructions: string; tools: string };
  definition: { instructions: string; tools: string[] | null }; source_path: string;
  profiles: AgentProfile[];
  tools: { name: string; description: string; parameters: Record<string, unknown>; category: string; available: boolean; reason: string }[];
  effective_tools?: string[]; inherited?: { instructions: string; tools: string[] };
  native_instructions?: string; workspace_instructions?: { path: string; text: string }[];
}
export interface VoiceProfile {
  enabled: boolean; model: ModelBinding; voice: string; instructions: string; context_mode: VoiceContextMode;
  backend_mode: "assistant" | "hosted"; backend_model: string;
}
export type VoiceDraft = Partial<VoiceProfile>;
export type NavigatorVoiceReply = Omit<LiveSettingsSnapshot, "scope" | "overrides" | "origins"> & {
  scope: NavigatorScope; agent?: string; overrides: VoiceDraft | LiveSettingsSnapshot["overrides"];
  effective?: VoiceProfile; inherited?: VoiceProfile; definition_effective?: VoiceProfile;
  definition_live?: VoiceDraft; saved_live_present?: boolean; agent_overrides?: VoiceDraft;
  origins: Partial<Record<keyof VoiceProfile, string>>;
  field_sources?: Partial<Record<keyof VoiceProfile, string>>;
};
export function voiceOrigin(reply: NavigatorVoiceReply, field: keyof VoiceProfile): string {
  const source = reply.field_sources?.[field];
  return source === "overlay" ? "agent" : source === "definition" ? "definition" : reply.origins[field] || "";
}
export function nestedVoice(reply: Pick<NavigatorVoiceReply, "values" | "effective">): VoiceProfile {
  const value = reply.values;
  return reply.effective || { enabled: value.enabled, model: { provider: value.connection_id || "", id: value.model },
    voice: value.voice, instructions: value.instructions || "", context_mode: value.context_mode || "recent", backend_mode: value.backend_mode, backend_model: value.backend_model };
}
export function voiceDraft(reply: NavigatorVoiceReply): VoiceDraft {
  if (reply.scope === "global") {
    const raw = reply.global_preferences;
    return { enabled: raw.enabled, model: { provider: raw.connection_id || "", id: raw.model }, voice: raw.voice,
      instructions: raw.instructions || "", context_mode: raw.context_mode || "recent", backend_mode: raw.backend_mode, backend_model: raw.backend_model };
  }
  if (reply.scope === "agent") return { ...(reply.agent_overrides || reply.overrides as VoiceDraft) };
  const values = reply.overrides as LiveSettingsSnapshot["overrides"];
  const result: VoiceDraft = {};
  for (const key of ["enabled", "voice", "instructions", "context_mode", "backend_mode", "backend_model"] as const)
    if (Object.hasOwn(values, key)) Object.assign(result, { [key]: values[key] });
  // Connection and model form one binding, even for legacy partial overrides.
  if (Object.hasOwn(values, "model") || Object.hasOwn(values, "connection_id")) result.model = { provider: values.connection_id ?? reply.global_preferences.connection_id ?? "", id: values.model ?? reply.global_preferences.model };
  return result;
}
export function voiceWrite(target: NavigatorTarget, revision: string, draft: VoiceDraft) {
  if (target.scope === "agent") return { scope: target.scope, agent: target.agent, revision, overrides: draft };
  const { model, ...rest } = draft;
  const preferences = { ...rest, ...(model ? { connection_id: model.provider, model: model.id } : {}) };
  return { scope: target.scope, revision, [target.scope === "global" ? "preferences" : "overrides"]: preferences };
}
export function inheritField<D extends object>(draft: D, key: keyof D): D { const next = { ...draft }; delete next[key]; return next; }
export function equal(left: unknown, right: unknown) { return JSON.stringify(left) === JSON.stringify(right); }
export async function readJson<T>(path: string, token: string, signal: AbortSignal): Promise<T> {
  return (await request(path, token, undefined, signal)).json() as Promise<T>;
}
export const settingsChanged = "ngn:agent-settings-saved";
export function announceSettingsSaved() { window.dispatchEvent(new window.Event(settingsChanged)); }

/** A retained draft owns its original revision until an explicit save/reload.
 * Read notifications refresh only clean editors; conflicts never replay writes. */
export function useNavigatorDraft<S extends { revision: string }, D extends object>({ read, write, create }: {
  read: (signal: AbortSignal) => Promise<S>; write: (snapshot: S, draft: D) => Promise<S>; create: (snapshot: S) => D;
}) {
  const [state, setState] = useState<{ snapshot?: S; draft?: D; pending: boolean; error: string; recovery: boolean; notice: string }>({ pending: true, error: "", recovery: false, notice: "" });
  const current = useRef(state); current.current = state;
  const api = useRef({ read, write, create }); api.current = { read, write, create };
  const abort = useRef<AbortController>(undefined);
  const busy = useRef(false);
  const alive = useRef(false);
  const dirty = !!state.snapshot && !!state.draft && !equal(state.draft, create(state.snapshot));
  function receive(snapshot: S, draft = api.current.create(snapshot), notice = "") {
    setState({ snapshot, draft, pending: false, error: "", recovery: false, notice });
  }
  async function reload() {
    if (busy.current) return;
    abort.current?.abort(); const controller = new AbortController(); abort.current = controller;
    setState(value => ({ ...value, pending: true, error: "" }));
    try { const reply = await api.current.read(controller.signal); if (!controller.signal.aborted) receive(reply); }
    catch (cause) { if (!controller.signal.aborted) setState(value => ({ ...value, pending: false, error: cause instanceof Error ? cause.message : "Could not load settings." })); }
  }
  useEffect(() => {
    alive.current = true; void reload();
    const refreshClean = () => {
      const value = current.current;
      if (!busy.current && !value.pending && !value.recovery && value.snapshot && value.draft && equal(value.draft, api.current.create(value.snapshot))) void reload();
    };
    window.addEventListener(settingsChanged, refreshClean);
    return () => { alive.current = false; abort.current?.abort(); window.removeEventListener(settingsChanged, refreshClean); };
  }, []);
  function edit(draft: D) { if (!busy.current) setState(value => ({ ...value, draft, notice: "" })); }
  async function save(posted = current.current.draft, retain?: (saved: D, before: D) => D, operation?: (snapshot: S) => Promise<S>) {
    const value = current.current;
    if (!value.snapshot || !value.draft || !posted || busy.current || value.pending || value.recovery) return;
    busy.current = true; abort.current?.abort();
    setState(previous => ({ ...previous, pending: true, error: "", notice: "" }));
    try {
      const reply = await (operation ? operation(value.snapshot) : api.current.write(value.snapshot, posted));
      if (!alive.current) return;
      receive(reply, retain ? retain(api.current.create(reply), value.draft) : api.current.create(reply), "Saved. Changes apply to the next activation.");
      // Do not re-read this resource before its success state commits.
      announceSettingsSaved();
    } catch (cause) {
      if (alive.current) { const failure = settingsFailure(cause, true); setState(previous => ({ ...previous, pending: false, error: failure.message, recovery: failure.needsRefresh || (cause instanceof RequestError && cause.status === 404) })); }
    } finally { busy.current = false; }
  }
  return { ...state, dirty, edit, save, reload, discard: () => { if (state.snapshot) setState(value => ({ ...value, draft: api.current.create(state.snapshot!), notice: "" })); } };
}
