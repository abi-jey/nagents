import { request, RequestError } from "../../api/client.js";
import type { LiveSettingsInput, LiveSettingsSnapshot, LiveSettingsValues, VoiceOverrides, VoicePreferences, VoiceScope } from "./types.js";

const preferenceKeys = ["enabled", "connection_id", "backend_mode", "model", "backend_model", "voice"] as const;
function preferences(values: LiveSettingsValues): VoicePreferences {
  return { enabled: values.enabled, ...(values.connection_id !== undefined ? { connection_id: values.connection_id } : {}), backend_mode: values.backend_mode, model: values.model, backend_model: values.backend_model, voice: values.voice };
}

export function settingsApi(token: string) {
  return {
    read: async (scope: VoiceScope, signal: AbortSignal): Promise<LiveSettingsSnapshot> =>
      (await request(`live/settings?scope=${scope}`, token, undefined, signal)).json() as Promise<LiveSettingsSnapshot>,
    save: async (body: LiveSettingsInput): Promise<LiveSettingsSnapshot> =>
      (await request("live/settings", token, body, AbortSignal.timeout(15_000))).json() as Promise<LiveSettingsSnapshot>,
  };
}

export function settingsValidation(values: LiveSettingsValues): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test(values.model) || !/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test(values.backend_model))
    return "Enter a voice and backend model ID (1–128 characters, without spaces).";
  return "";
}

export interface LiveSettingsState {
  snapshot?: LiveSettingsSnapshot;
  values?: LiveSettingsValues;
  overrides: VoiceOverrides;
  scope: VoiceScope;
  loading: boolean;
  saving: boolean;
  error: string;
  needsRefresh: boolean;
  dirty: boolean;
}

export class LiveSettingsController {
  private state: LiveSettingsState = { loading: true, saving: false, error: "", needsRefresh: false, dirty: false, overrides: {}, scope: "workspace" };
  private listeners = new Set<() => void>();
  private abort?: AbortController;
  private epoch = 0;
  private disposed = false;
  constructor(private readonly api: ReturnType<typeof settingsApi>) {}
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private update(next: Partial<LiveSettingsState>) {
    this.state = { ...this.state, ...next };
    this.listeners.forEach((listener) => listener());
  }
  private receive(snapshot: LiveSettingsSnapshot) {
    this.update({ snapshot, values: { ...snapshot.values }, overrides: { ...snapshot.overrides }, error: "", needsRefresh: false, dirty: false });
  }
  setScope(scope: VoiceScope) {
    if (scope === this.state.scope || this.state.saving || this.state.dirty) return;
    this.abort?.abort(); ++this.epoch;
    this.update({ scope, snapshot: undefined, values: undefined, overrides: {}, loading: true, error: "", needsRefresh: false });
    void this.load();
  }
  async load(): Promise<void> {
    if (this.disposed || this.state.saving) return;
    this.abort?.abort(); const abort = new AbortController(); this.abort = abort;
    const epoch = ++this.epoch;
    this.update({ loading: true, error: "" });
    try {
      const snapshot = await this.api.read(this.state.scope, AbortSignal.any([abort.signal, AbortSignal.timeout(10_000)]));
      if (epoch === this.epoch) this.receive(snapshot);
    } catch (cause) {
      if (epoch === this.epoch) this.update({ error: cause instanceof Error ? cause.message : "Could not load Voice settings." });
    } finally { if (epoch === this.epoch) this.update({ loading: false }); }
  }
  edit(patch: Partial<LiveSettingsValues>) {
    if (!this.state.values || this.state.loading || this.state.saving) return;
    const values = { ...this.state.values, ...patch };
    const overrides = { ...this.state.overrides };
    if (this.state.scope === "workspace") for (const key of preferenceKeys) {
      if (Object.hasOwn(patch, key) && Object.hasOwn(overrides, key)) {
        // Each override is independent; editing one never freezes the other inherited fields.
        Object.assign(overrides, { [key]: values[key] });
      }
    }
    this.update({ values, overrides, dirty: true });
  }
  override(key: keyof VoicePreferences, enabled: boolean) {
    const { snapshot, values, overrides, scope, loading, saving } = this.state;
    if (!snapshot || !values || scope !== "workspace" || loading || saving) return;
    const next = { ...overrides };
    if (enabled) Object.assign(next, { [key]: key === "connection_id" ? values.connection_id || "" : values[key] });
    else delete next[key];
    this.update({ overrides: next, values: enabled ? values : { ...values, [key]: snapshot.global_preferences[key] }, dirty: true });
  }
  async save(): Promise<LiveSettingsSnapshot | undefined> {
    const { snapshot, values, overrides, scope, saving, loading, needsRefresh } = this.state;
    if (this.disposed || !snapshot || !values || saving || loading || needsRefresh) return;
    const error = settingsValidation(values);
    if (error) { this.update({ error }); return; }
    const epoch = ++this.epoch;
    this.update({ saving: true, error: "" });
    try {
      const body: LiveSettingsInput = scope === "global"
        ? { scope, revision: snapshot.revision, preferences: preferences(values) }
        : { scope, revision: snapshot.revision, overrides: { ...overrides } };
      const result = await this.api.save(body);
      if (epoch !== this.epoch) return;
      this.receive(result);
      return result;
    } catch (cause) {
      if (epoch === this.epoch) this.update({
        error: cause instanceof RequestError ? cause.message : "Save could not be confirmed. Reload Voice settings before trying again.",
        needsRefresh: !(cause instanceof RequestError && cause.status === 422),
      });
    } finally { if (epoch === this.epoch) this.update({ saving: false }); }
  }
  dispose() { this.disposed = true; ++this.epoch; this.abort?.abort(); this.listeners.clear(); }
}
