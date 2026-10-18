import { useSyncExternalStore } from "react";

export const activityCategories = ["shell", "files", "reasoning", "agents", "notices", "tools"] as const;
export type ActivityCategory = typeof activityCategories[number];
export type ActivityPlacement = "hidden" | "grouped" | "separate";
export type DetailExpansion = "collapsed" | "expanded";
export interface ActivityAppearance { placement: ActivityPlacement; details: DetailExpansion; }
export interface ChatAppearance {
  version: 1;
  activity: Record<ActivityCategory, ActivityAppearance>;
  runUsage: "hidden" | DetailExpansion;
}
const rule = (placement: ActivityPlacement, details: DetailExpansion = "collapsed"): ActivityAppearance => ({ placement, details });
const preset = (placement: ActivityPlacement, details: DetailExpansion, runUsage: ChatAppearance["runUsage"]): ChatAppearance => ({
  version: 1, activity: Object.fromEntries(activityCategories.map(category => [category, rule(placement, details)])) as ChatAppearance["activity"], runUsage,
});
const compact = preset("grouped", "collapsed", "collapsed");
export const appearancePresets = [
  { id: "messages", label: "Messages only", description: "Keep the conversation to messages and delivered files.", value: preset("hidden", "collapsed", "hidden") },
  { id: "quiet", label: "Quiet", description: "Keep file changes and your team in small groups. Tuck other activity away.", value: { ...preset("hidden", "collapsed", "collapsed"), activity: { ...preset("hidden", "collapsed", "collapsed").activity, files: rule("grouped"), agents: rule("grouped") } } },
  { id: "compact", label: "Compact", description: "Group activity into small rows. Open reasoning and details when you need them.", value: compact },
  { id: "detailed", label: "Detailed", description: "Give commands and file changes their own space, with their details open.", value: { ...compact, activity: { ...compact.activity, shell: rule("separate", "expanded"), files: rule("separate", "expanded"), agents: rule("separate") } } },
  { id: "everything", label: "Everything", description: "Show activity separately, with reasoning, results, and run details open.", value: preset("separate", "expanded", "expanded") },
] satisfies { id: string; label: string; description: string; value: ChatAppearance }[];
export const defaultAppearance: ChatAppearance = compact;
export const appearanceStorageKey = "ngn:chat-appearance:v1";
const changed = "ngn:chat-appearance-changed";
let cachedWindow: Window | undefined;
let cachedRaw: string | null | undefined;
let cached = defaultAppearance;
let memory = defaultAppearance;

export function parseAppearance(raw: string | null): ChatAppearance {
  if (!raw || raw.length > 16384) return defaultAppearance;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || !("version" in value) || value.version !== 1 || !("activity" in value) ||
      !value.activity || typeof value.activity !== "object") return defaultAppearance;
    const activity = { ...defaultAppearance.activity };
    for (const category of activityCategories) {
      const item: unknown = (value.activity as Record<string, unknown>)[category];
      if (!item || typeof item !== "object" || !("placement" in item) || !("details" in item)) continue;
      if (typeof item.placement !== "string" || !["hidden", "grouped", "separate"].includes(item.placement) ||
        typeof item.details !== "string" || !["collapsed", "expanded"].includes(item.details)) continue;
      activity[category] = { placement: item.placement as ActivityPlacement, details: item.details as DetailExpansion };
    }
    const runUsage = "runUsage" in value ? value.runUsage : undefined;
    return { version: 1, activity, runUsage: typeof runUsage === "string" && ["hidden", "collapsed", "expanded"].includes(runUsage)
      ? runUsage as ChatAppearance["runUsage"] : defaultAppearance.runUsage };
  } catch { return defaultAppearance; }
}
export function selectedAppearancePreset(value: ChatAppearance) {
  return appearancePresets.find(preset => preset.value.runUsage === value.runUsage && activityCategories.every(category =>
    preset.value.activity[category].placement === value.activity[category].placement &&
    (value.activity[category].placement === "hidden" || preset.value.activity[category].details === value.activity[category].details)));
}
function snapshot(): ChatAppearance {
  if (typeof window === "undefined") return defaultAppearance;
  if (cachedWindow !== window) { cachedWindow = window; cachedRaw = undefined; memory = defaultAppearance; }
  try {
    const raw = window.localStorage.getItem(appearanceStorageKey);
    if (raw !== cachedRaw) { cachedRaw = raw; cached = memory = parseAppearance(raw); }
    return cached;
  } catch { return memory; }
}
export function saveAppearance(value: ChatAppearance): boolean {
  if (typeof window === "undefined") return false;
  snapshot(); memory = value;
  const raw = JSON.stringify(value);
  cached = value;
  let saved = true;
  try { window.localStorage.setItem(appearanceStorageKey, raw); cachedRaw = raw; }
  catch { saved = false; }
  window.dispatchEvent(new window.Event(changed));
  return saved;
}
function subscribe(listener: () => void) {
  window.addEventListener(changed, listener);
  window.addEventListener("storage", listener);
  return () => { window.removeEventListener(changed, listener); window.removeEventListener("storage", listener); };
}
export function useAppearance(): ChatAppearance { return useSyncExternalStore(subscribe, snapshot, () => defaultAppearance); }
