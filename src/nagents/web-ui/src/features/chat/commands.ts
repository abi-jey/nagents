import type { SkillItem } from "./skills.js";

/** Builtins stay local; explicit skills use ordinary server-owned message admission. */
export const webCommands = [
  { name: "help", description: "Show browser commands", argument: "" },
  { name: "login", description: "Sign in to ChatGPT / Codex on this server", argument: "" },
  { name: "skills", description: "Browse skills available to this agent", argument: "[search]" },
  { name: "new", description: "Start a new conversation", argument: "" },
  { name: "fork", description: "Branch from this conversation's saved context", argument: "[title]" },
  { name: "rename", description: "Rename this conversation", argument: "<title>" },
  { name: "sessions", description: "Browse or resume conversations", argument: "[session ID]" },
  { name: "compact", description: "Summarize the current conversation", argument: "" },
  { name: "model", description: "Choose the workspace model", argument: "[model ID]" },
  { name: "agent", description: "Choose this chat's agent; --workspace sets the default", argument: "[--workspace] [name]" },
  { name: "provider", description: "Choose a provider connection", argument: "[name]" },
  { name: "context", description: "Show the conversation context estimate", argument: "" },
  { name: "settings", description: "Open workspace settings", argument: "" },
  { name: "tools", description: "Choose available tools", argument: "" },
  { name: "channels", description: "Manage connected channels", argument: "" },
  { name: "voice", description: "Open voice settings", argument: "" },
  { name: "stop", description: "Stop the active run", argument: "" },
] as const;
export type WebCommand = typeof webCommands[number];
export type CommandName = WebCommand["name"];
export type CommandIntent = { name: CommandName; argument: string };
export type SkillIntent = { name: "skill"; invocation: string };
export type SlashCommand = { name: string; description: string; argument: string; skill?: string };

export function commandIntent(value: string): CommandIntent | undefined;
export function commandIntent(value: string, allowSkills: true): CommandIntent | SkillIntent | undefined;
export function commandIntent(value: string, allowSkills = false): CommandIntent | SkillIntent | undefined {
  const text = value.trim();
  if (!text.startsWith("/")) return undefined;
  const [token, ...parts] = text.split(/\s+/);
  const name = token === "/resume" ? "sessions" : token === "/" ? "help" : token.slice(1);
  const command = webCommands.find(command => command.name === name);
  if (!command) {
    if (allowSkills && /^(?:skill:)?[A-Za-z0-9_-]{1,64}$/.test(name)) return { name: "skill", invocation: value };
    throw new Error(`Unknown browser command: ${token}. Use /help for commands or /skills to browse skills.`);
  }
  const argument = parts.join(" ");
  if (argument && !command.argument) throw new Error(`Usage: /${command.name}. Your draft is kept.`);
  if (name === "rename" && !argument) throw new Error("Usage: /rename <title>. Your draft is kept.");
  return { name: command.name, argument };
}

export function skillCommand(skill: SkillItem): string {
  return webCommands.some(command => command.name === skill.name) || skill.name === "resume" ? skill.canonical_command : skill.command;
}

export function commandMatches(value: string, skills: readonly SkillItem[] = []): readonly SlashCommand[] {
  if (!/^\/[A-Za-z0-9:_-]*$/.test(value)) return [];
  const query = value.slice(1).toLowerCase();
  return [...webCommands, ...skills.filter(skill => skill.enabled).map(skill => ({ name: query.startsWith("skill:") ? skill.canonical_command : skillCommand(skill),
    description: skill.description, argument: "[request]", skill: skill.name }))].filter(command => command.name.toLowerCase().startsWith(query));
}
