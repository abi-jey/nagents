import { request, RequestError } from "../../api/client.js";
import type { LiveSettingsInput, LiveSettingsSnapshot, LiveSettingsValues } from "./types.js";

export function settingsApi(token: string) {
  return {
    read: async (signal: AbortSignal): Promise<LiveSettingsSnapshot> =>
      (await request("live/settings", token, undefined, signal)).json() as Promise<LiveSettingsSnapshot>,
    save: async (body: LiveSettingsInput): Promise<LiveSettingsSnapshot> =>
      (await request("live/settings", token, body, AbortSignal.timeout(15_000))).json() as Promise<LiveSettingsSnapshot>,
  };
}

export function settingsValidation(values: LiveSettingsValues): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test(values.model) || !/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test(values.backend_model))
    return "Enter a voice and backend model ID (1–128 characters, without spaces).";
  if (values.provider === "azure_openai_compatible_v1" && !values.base_url.trim()) return "Enter the Azure API endpoint.";
  if (values.base_url) {
    try {
      const url = new URL(values.base_url);
      if ((url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))) || url.username || url.password || url.search || url.hash)
        return "Use an HTTPS API prefix without credentials or query parameters (loopback HTTP is supported).";
    } catch { return "Enter a valid API endpoint URL."; }
  }
  return "";
}

export interface LiveSettingsState {
  snapshot?: LiveSettingsSnapshot;
  values?: LiveSettingsValues;
  loading: boolean;
  saving: boolean;
  error: string;
  needsRefresh: boolean;
}

export class LiveSettingsController {
  private state: LiveSettingsState = { loading: true, saving: false, error: "", needsRefresh: false };
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
    this.update({ snapshot, values: { ...snapshot.values }, error: "", needsRefresh: false });
  }
  async load(): Promise<void> {
    if (this.disposed || this.state.saving) return;
    this.abort?.abort(); const abort = new AbortController(); this.abort = abort;
    const epoch = ++this.epoch;
    this.update({ loading: true, error: "" });
    try {
      const snapshot = await this.api.read(AbortSignal.any([abort.signal, AbortSignal.timeout(10_000)]));
      if (epoch === this.epoch) this.receive(snapshot);
    } catch (cause) {
      if (epoch === this.epoch) this.update({ error: cause instanceof Error ? cause.message : "Could not load connection settings." });
    } finally { if (epoch === this.epoch) this.update({ loading: false }); }
  }
  edit(patch: Partial<LiveSettingsValues>) {
    if (!this.state.values || this.state.loading || this.state.saving) return;
    this.update({ values: { ...this.state.values, ...patch } });
  }
  async save(): Promise<LiveSettingsSnapshot | undefined> {
    const { snapshot, values, saving, loading, needsRefresh } = this.state;
    if (this.disposed || !snapshot || !values || saving || loading || needsRefresh) return;
    const error = settingsValidation(values);
    if (error) { this.update({ error }); return; }
    const epoch = ++this.epoch;
    this.update({ saving: true, error: "" });
    try {
      const result = await this.api.save({ revision: snapshot.revision, values: { ...values } });
      if (epoch !== this.epoch) return;
      this.receive(result);
      return result;
    } catch (cause) {
      if (epoch === this.epoch) this.update({
        error: cause instanceof RequestError ? cause.message : "Save could not be confirmed. Reload connection settings before trying again.",
        needsRefresh: !(cause instanceof RequestError && cause.status === 422),
      });
    } finally { if (epoch === this.epoch) this.update({ saving: false }); }
  }
  dispose() { this.disposed = true; ++this.epoch; this.abort?.abort(); this.listeners.clear(); }
}
