/** Web-native commands. Model prompts and local actions never share a dispatch path. */
export const webCommands = [
  { name: "help", description: "Show browser commands", argument: "" },
  { name: "login", description: "Sign in to ChatGPT / Codex on this server", argument: "" },
  { name: "new", description: "Start a new conversation", argument: "" },
  { name: "fork", description: "Branch from this conversation's saved context", argument: "[title]" },
  { name: "rename", description: "Rename this conversation", argument: "<title>" },
  { name: "sessions", description: "Browse or resume conversations", argument: "[session ID]" },
  { name: "compact", description: "Summarize the current conversation", argument: "" },
  { name: "model", description: "Choose the workspace model", argument: "[model ID]" },
  { name: "agent", description: "Choose the agent profile", argument: "[name]" },
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

export function commandIntent(value: string): CommandIntent | undefined {
  const text = value.trim();
  if (!text.startsWith("/")) return undefined;
  const [token, ...parts] = text.split(/\s+/);
  const name = token === "/resume" ? "sessions" : token === "/" ? "help" : token.slice(1);
  const command = webCommands.find(command => command.name === name);
  if (!command) throw new Error(`Unknown browser command: ${token}. Use /help to see available commands.`);
  const argument = parts.join(" ");
  if (argument && !command.argument) throw new Error(`Usage: /${command.name}. Your draft is kept.`);
  if (name === "rename" && !argument) throw new Error("Usage: /rename <title>. Your draft is kept.");
  return { name: command.name, argument };
}

export function commandMatches(value: string): readonly WebCommand[] {
  if (!/^\/[a-z]*$/.test(value)) return [];
  const query = value.slice(1);
  return webCommands.filter(command => command.name.startsWith(query));
}
