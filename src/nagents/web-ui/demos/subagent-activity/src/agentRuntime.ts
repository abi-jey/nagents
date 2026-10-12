import { definitions, MAIN_AGENT_DEFINITION } from "./fixtures";
import type { ActivityState, AgentDefinition, AgentId, AgentRun } from "./types";

export type CompactionTrigger = "auto" | "tokens" | "messages" | "off";
export type DemoProviderId = "openai" | "anthropic" | "gemini";

/** An omitted field inherits the agent's original profile. Saved values are next-run values. */
export interface RuntimeSettings {
  provider?: DemoProviderId;
  model?: string;
  compact_trigger?: CompactionTrigger;
  compact_tokens?: number;
  compact_messages?: number;
}
export type RuntimeSettingsMap = Record<AgentId, RuntimeSettings>;
export interface DemoModel { id: string; label: string; contextLimit: number }
export interface DemoProvider { id: DemoProviderId; label: string; models: readonly DemoModel[] }

/** Design fixtures, not live discovery or claims about provider model capabilities. */
export const DEMO_PROVIDERS: readonly DemoProvider[] = [
  { id: "openai", label: "OpenAI", models: [
    { id: "gpt-6-luna", label: "gpt-6-luna", contextLimit: 200_000 },
    { id: "gpt-6.1-sol", label: "gpt-6.1-sol", contextLimit: 200_000 },
    { id: "gpt-6-astra", label: "gpt-6-astra", contextLimit: 200_000 },
  ] },
  { id: "anthropic", label: "Anthropic", models: [
    { id: "demo-claude", label: "Claude · illustrative model", contextLimit: 200_000 },
  ] },
  { id: "gemini", label: "Google Gemini", models: [
    { id: "demo-gemini", label: "Gemini · illustrative model", contextLimit: 128_000 },
  ] },
];
export const DEMO_CATALOG_NOTE = "Illustrative catalog and context windows. This demo does not discover models or call providers.";
const COMPACTION_TRIGGERS: readonly CompactionTrigger[] = ["auto", "tokens", "messages", "off"];
const FIXTURE_PROMPT_TOKENS: Readonly<Record<AgentId, number>> = { assistant: 18_000, research: 8_000, builder: 12_000, layout: 4_000, tests: 10_000 };
const DEFAULT_TOKEN_BUDGET = 200_000;
const DEFAULT_MESSAGE_LIMIT = 100;

export interface ResolvedRuntimeSettings {
  provider: DemoProviderId;
  providerLabel: string;
  model: string;
  modelContextLimit: number;
  compact_trigger: CompactionTrigger;
  compact_tokens: number;
  compact_messages: number;
}
export interface RuntimeValidation {
  valid: boolean;
  errors: Partial<Record<keyof RuntimeSettings, string>>;
}
export interface AgentRuntime {
  providerId: DemoProviderId;
  providerLabel: string;
  model: string;
  estimatedTokens: number;
  modelContextLimit: number;
  contextFraction: number;
  contextOverflow: boolean;
  estimateDetail: string;
  messageCount: number;
  toolCount: number;
  queuedMessages: number;
  promptBaselineTokens: number;
  compactionLabel: string;
  compactionDetail: string;
  compactThresholdTokens?: number;
  compactThresholdMessages?: number;
  compactionDue: boolean;
  compactionStatus: "disabled" | "below_threshold" | "due_next_response";
  settings: ResolvedRuntimeSettings;
}

export function createRuntimeSettings(): RuntimeSettingsMap { return {}; }
export function copyRuntimeSettings(settings: RuntimeSettingsMap): RuntimeSettingsMap {
  return Object.fromEntries(Object.entries(settings).map(([id, value]) => [id, { ...value }]));
}

function originalDefinition(agent: AgentRun | AgentDefinition): AgentDefinition {
  const definition = "definition" in agent ? agent.definition : agent;
  if (definition.id === MAIN_AGENT_DEFINITION.id) return MAIN_AGENT_DEFINITION;
  return definitions.find(candidate => candidate.id === definition.id) ?? definition;
}
function inheritedProvider(definition: AgentDefinition): DemoProvider {
  return DEMO_PROVIDERS.find(provider => provider.label === definition.provider || provider.id === definition.provider) ?? DEMO_PROVIDERS[0];
}
function selectedProvider(definition: AgentDefinition, settings: RuntimeSettings): DemoProvider {
  return DEMO_PROVIDERS.find(provider => provider.id === settings.provider) ?? inheritedProvider(definition);
}
export function validateRuntimeSettings(agent: AgentRun | AgentDefinition, settings: RuntimeSettings): RuntimeValidation {
  const errors: RuntimeValidation["errors"] = {};
  const definition = originalDefinition(agent);
  const provider = selectedProvider(definition, settings);
  if (settings.provider !== undefined && !DEMO_PROVIDERS.some(candidate => candidate.id === settings.provider)) errors.provider = "Choose a provider from this demo catalog.";
  if (settings.model !== undefined && !provider.models.some(model => model.id === settings.model)) errors.model = "Choose a model for the selected provider, or inherit its default.";
  if (settings.compact_trigger !== undefined && !COMPACTION_TRIGGERS.includes(settings.compact_trigger)) errors.compact_trigger = "Choose an available compaction policy.";
  if (settings.compact_tokens !== undefined && (!Number.isInteger(settings.compact_tokens) || settings.compact_tokens < 1024 || settings.compact_tokens > 10_000_000)) errors.compact_tokens = "Enter a whole-number token budget from 1,024 to 10,000,000.";
  if (settings.compact_messages !== undefined && (!Number.isInteger(settings.compact_messages) || settings.compact_messages < 1 || settings.compact_messages > 10_000)) errors.compact_messages = "Enter a whole-number message limit from 1 to 10,000.";
  return { valid: Object.keys(errors).length === 0, errors };
}
export function resolveRuntimeSettings(agent: AgentRun | AgentDefinition, overrides: RuntimeSettings = {}): ResolvedRuntimeSettings {
  const validation = validateRuntimeSettings(agent, overrides);
  if (!validation.valid) throw new RangeError(Object.values(validation.errors).join(" "));
  const definition = originalDefinition(agent);
  const provider = selectedProvider(definition, overrides);
  const inheritedModel = provider.id === inheritedProvider(definition).id ? definition.model : provider.models[0].id;
  const model = provider.models.find(candidate => candidate.id === (overrides.model ?? inheritedModel)) ?? provider.models[0];
  return {
    provider: provider.id, providerLabel: provider.label, model: model.id, modelContextLimit: model.contextLimit,
    compact_trigger: overrides.compact_trigger ?? "auto",
    compact_tokens: overrides.compact_tokens ?? DEFAULT_TOKEN_BUDGET,
    compact_messages: overrides.compact_messages ?? DEFAULT_MESSAGE_LIMIT,
  };
}

/** Call only on scenario restart; never rewrite an already accepted run in place. */
export function applyLaunchSettings(state: ActivityState, settings: RuntimeSettingsMap): ActivityState {
  if (state.now !== 0 || Object.values(state.agents).some(agent => agent.records.length > 0)) throw new Error("Apply next-run settings only to a fresh scenario.");
  const agents: ActivityState["agents"] = {};
  for (const [id, agent] of Object.entries(state.agents)) {
    const resolved = resolveRuntimeSettings(agent, settings[id]);
    agents[id] = { ...agent, definition: { ...agent.definition, provider: resolved.providerLabel, model: resolved.model } };
  }
  return { ...state, agents };
}

function estimatedTextTokens(text: string): number { return Math.ceil(new TextEncoder().encode(text).length / 4); }
function formatCount(value: number): string { return value.toLocaleString("en-US"); }

/** Pass the accepted settings snapshot, not settings saved for the next scenario. */
export function resolveAgentRuntime(agent: AgentRun, state: ActivityState, acceptedSettings: RuntimeSettingsMap): AgentRuntime {
  const settings = resolveRuntimeSettings(agent, acceptedSettings[agent.definition.id]);
  const spawned = state.now >= agent.definition.spawnedAt;
  const promptBaselineTokens = spawned ? FIXTURE_PROMPT_TOKENS[agent.definition.id] ?? 2_000 : 0;
  let estimated = promptBaselineTokens + (spawned ? estimatedTextTokens(agent.definition.assignment) : 0);
  let messageCount = spawned ? 1 : 0;
  let toolCount = 0;
  let queuedMessages = 0;
  for (const record of spawned ? agent.records : []) {
    if (record.kind === "message") {
      if (record.message.delivery === "queued") { queuedMessages += 1; continue; }
      estimated += estimatedTextTokens(record.message.text);
      messageCount += 1;
    } else {
      estimated += estimatedTextTokens(JSON.stringify(record.tool.arguments));
      messageCount += 1;
      toolCount += 1;
      // A partial output is visible evidence, not a completed result in model context.
      if (record.tool.status === "completed" || record.tool.status === "failed") {
        estimated += estimatedTextTokens(record.tool.output);
        messageCount += 1;
      }
    }
  }
  const compactThresholdTokens = settings.compact_trigger === "auto" ? Math.floor(settings.modelContextLimit * 0.7)
    : settings.compact_trigger === "tokens" ? Math.floor(settings.compact_tokens * 0.7) : undefined;
  const compactThresholdMessages = settings.compact_trigger === "messages" ? settings.compact_messages : undefined;
  const compactionDue = compactThresholdTokens !== undefined ? estimated >= compactThresholdTokens
    : compactThresholdMessages !== undefined && messageCount >= compactThresholdMessages;
  const compactionLabel = settings.compact_trigger === "off" ? "Compaction off"
    : settings.compact_trigger === "messages" ? `At ${formatCount(settings.compact_messages)} messages`
      : `At ${formatCount(compactThresholdTokens ?? 0)} tokens`;
  const policyDetail = settings.compact_trigger === "off" ? "Automatic compaction is disabled for this run."
    : settings.compact_trigger === "messages" ? `${formatCount(messageCount)} of ${formatCount(settings.compact_messages)} demo context messages.`
      : settings.compact_trigger === "auto" ? "Auto · 70% of the illustrative model context window."
        : `70% of the ${formatCount(settings.compact_tokens)}-token configured budget; the model window is separate.`;
  const compactionDetail = `${policyDetail}${compactThresholdTokens !== undefined && compactThresholdTokens > settings.modelContextLimit ? " This trigger exceeds the illustrative model window; that window is reached first." : ""}${compactionDue ? " Due before the next model response, after the active tool batch. This demo does not perform compaction." : ""}`;
  return {
    providerId: settings.provider, providerLabel: settings.providerLabel, model: settings.model,
    estimatedTokens: estimated, modelContextLimit: settings.modelContextLimit,
    contextFraction: Math.min(1, estimated / settings.modelContextLimit), contextOverflow: estimated > settings.modelContextLimit,
    estimateDetail: `Demo estimate: ${formatCount(promptBaselineTokens)} fixture prompt tokens plus assignment, accepted messages, tool arguments, and completed results (UTF-8 bytes ÷ 4). Queued notes and partial tool output are excluded.`,
    messageCount, toolCount, queuedMessages, promptBaselineTokens, compactionLabel, compactionDetail,
    compactThresholdTokens, compactThresholdMessages, compactionDue,
    compactionStatus: settings.compact_trigger === "off" ? "disabled" : compactionDue ? "due_next_response" : "below_threshold",
    settings,
  };
}
