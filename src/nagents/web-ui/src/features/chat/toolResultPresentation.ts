// Formatting is deliberately bounded and lexical: parsing and reserializing
// JSON would round large integer IDs and discard duplicate object keys.
const FORMAT_LIMIT = 128 * 1024;
const TOKEN_LIMIT = 32_768;
const DEPTH_LIMIT = 32;
const DISPLAY_LIMIT = 32 * 1024;
const tokenPattern = /"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|[{}\[\],:]/g;

export type ResultSection = { label: string; text: string; kind?: "diff" | "list" };
export type ResultPresentation = {
  format: string;
  text: string;
  structured: boolean;
  note?: string;
  sections?: ResultSection[];
  indicators?: { text: string; warning: boolean }[];
  metadata?: { label: string; text: string }[];
  diagnostics?: { label: string; text: string }[];
};

function formatJSON(text: string): string | undefined {
  const tokens: string[] = [];
  for (const match of text.matchAll(tokenPattern)) {
    if (tokens.length === TOKEN_LIMIT) return undefined;
    tokens.push(match[0]);
  }
  const output: string[] = [];
  let depth = 0;
  const newline = () => output.push(`\n${"  ".repeat(depth)}`);
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index];
    if (token === "{" || token === "[") {
      output.push(token);
      depth++;
      if (depth > DEPTH_LIMIT) return undefined;
      if (tokens[index + 1] !== "}" && tokens[index + 1] !== "]") newline();
    } else if (token === "}" || token === "]") {
      depth--;
      if (tokens[index - 1] !== "{" && tokens[index - 1] !== "[") newline();
      output.push(token);
    } else if (token === ",") {
      output.push(token);
      newline();
    } else output.push(token === ":" ? ": " : token);
  }
  return output.join("");
}

function unsafeFields(text: string): boolean {
  const containers: (Set<string> | undefined)[] = [];
  for (const match of text.matchAll(tokenPattern)) {
    const token = match[0];
    if (token === "{" || token === "[") containers.push(token === "{" ? new Set() : undefined);
    else if (token === "}" || token === "]") containers.pop();
    else if (token.startsWith('"') && /^\s*:/.test(text.slice(match.index + token.length))) {
      const key = JSON.parse(token) as string;
      const keys = containers.at(-1);
      if (keys?.has(key)) return true;
      keys?.add(key);
    } else if (/^-?\d/.test(token)) {
      const number = Number(token);
      if (!Number.isFinite(number) || (Number.isInteger(number) && !Number.isSafeInteger(number))) return true;
    }
  }
  return false;
}

// Native views must never reinterpret duplicate keys or rounded integer IDs.
// The fallback formatter remains lexical, retaining every original token.
export function toolObject(text: string): Record<string, unknown> | undefined {
  if (text.length > FORMAT_LIMIT) return undefined;
  try {
    const value: unknown = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value)
      || formatJSON(text) === undefined || unsafeFields(text)) return undefined;
    return value as Record<string, unknown>;
  } catch { return undefined; }
}

function commandPresentation(value: unknown, source: string): Pick<ResultPresentation, "sections" | "indicators"> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  const allowed = new Set(["output", "stdout", "stderr", "exit_code", "timed_out", "truncated"]);
  // An unfamiliar field or field type falls back to complete JSON. Never
  // discard provider diagnostics just because a result resembles shell output.
  if (Object.keys(record).some((key) => !allowed.has(key))) return undefined;
  for (const key of ["output", "stdout", "stderr"])
    if (Object.hasOwn(record, key) && typeof record[key] !== "string") return undefined;
  for (const key of ["timed_out", "truncated"])
    if (Object.hasOwn(record, key) && typeof record[key] !== "boolean") return undefined;
  if (Object.hasOwn(record, "exit_code") && (typeof record.exit_code !== "number" || !Number.isSafeInteger(record.exit_code))) return undefined;
  const sections: ResultSection[] = [];
  for (const [key, label] of [["output", "Output"], ["stdout", "Standard output"], ["stderr", "Standard error"]])
    if (typeof record[key] === "string") sections.push({ label, text: record[key] });
  if (!sections.length) return undefined;
  if (unsafeFields(source)) return undefined;
  const indicators = [];
  if (typeof record.exit_code === "number") indicators.push({ text: `Exit ${record.exit_code}`, warning: record.exit_code !== 0 });
  if (record.timed_out === true) indicators.push({ text: "Timed out", warning: true });
  if (record.truncated === true) indicators.push({ text: "Output truncated by tool", warning: true });
  return { sections, indicators };
}

export function presentToolResult(text: string, toolName = ""): ResultPresentation {
  const plain: ResultPresentation = { format: "Text", text, structured: false };
  if (!text) return plain;
  if (text.length > FORMAT_LIMIT) return { ...plain, note: "Large output is shown without reformatting." };
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value === "string") {
      // Decode one JSON string wrapper, without recursively interpreting text.
      let decoded = value;
      let structured = false;
      try {
        const inner: unknown = JSON.parse(value);
        if (inner && typeof inner === "object") {
          const formatted = formatJSON(value);
          if (formatted !== undefined) { decoded = formatted; structured = true; }
        }
      } catch { /* Ordinary string content. */ }
      return { format: "JSON string", text: decoded, structured };
    }
    const formatted = formatJSON(text);
    if (formatted === undefined) return { ...plain, format: "JSON", note: "Complex JSON is shown without reformatting." };
    const command = ["shell", "run_shell_command"].includes(toolName) ? commandPresentation(value, text) : undefined;
    return { format: command ? "Command output" : "JSON", text: formatted, structured: true, ...command };
  } catch { /* Try newline-delimited JSON before retaining ordinary output. */ }
  const lines = text.split("\n").filter((line) => line.trim());
  if (lines.length < 2 || lines.length > 1024) return plain;
  const formatted: string[] = [];
  for (const line of lines) {
    try { JSON.parse(line); } catch { return plain; }
    const value = formatJSON(line);
    if (value === undefined) return plain;
    formatted.push(value);
  }
  return { format: `JSON Lines · ${lines.length} records`, text: formatted.join("\n\n"), structured: true };
}

export function visibleToolText(text: string, limit = DISPLAY_LIMIT): { text: string; omitted: number } {
  if (text.length <= limit) return { text, omitted: 0 };
  // Keep the tail visible: exit errors and truncation notices commonly arrive
  // there. Copy always uses the untouched source, not this display excerpt.
  const tail = Math.min(8192, Math.floor(limit / 4));
  const omitted = text.length - limit;
  return { text: `${text.slice(0, limit - tail)}\n\n… ${omitted.toLocaleString("en-US")} characters between these excerpts …\n\n${text.slice(-tail)}`, omitted };
}

export type ResultToken = { text: string; kind: "plain" | "key" | "string" | "number" | "literal" };

export function toolResultTokens(text: string): ResultToken[] {
  if (text.length > 16_384) return [{ text, kind: "plain" }];
  const tokens: ResultToken[] = [];
  let position = 0;
  for (const match of text.matchAll(tokenPattern)) {
    if (tokens.length > 2000) return [{ text, kind: "plain" }];
    if (match.index > position) tokens.push({ text: text.slice(position, match.index), kind: "plain" });
    const value = match[0];
    const end = match.index + value.length;
    const kind = value.startsWith('"') ? /^\s*:/.test(text.slice(end)) ? "key" : "string"
      : /^-?\d/.test(value) ? "number" : ["null", "true", "false"].includes(value) ? "literal" : "plain";
    tokens.push({ text: value, kind });
    position = end;
  }
  if (position < text.length) tokens.push({ text: text.slice(position), kind: "plain" });
  return tokens;
}
