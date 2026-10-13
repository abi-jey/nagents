import type { ReactNode } from "react";
import { createLowlight } from "lowlight";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import shell from "highlight.js/lib/languages/shell";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

// Import only the grammars we support. No fence-language detection or runtime downloads.
const highlighter = createLowlight({ bash, css, diff, javascript, json, python, shell, sql, typescript, xml, yaml });
const languages = new Map([
  ["bash", "bash"], ["sh", "bash"], ["zsh", "bash"], ["shell", "bash"],
  ["console", "shell"], ["shellsession", "shell"],
  ["css", "css"], ["diff", "diff"], ["patch", "diff"],
  ["javascript", "javascript"], ["js", "javascript"], ["jsx", "javascript"], ["mjs", "javascript"], ["cjs", "javascript"],
  ["json", "json"], ["jsonc", "json"],
  ["python", "python"], ["py", "python"], ["python3", "python"],
  ["sql", "sql"],
  ["typescript", "typescript"], ["ts", "typescript"], ["tsx", "typescript"], ["mts", "typescript"], ["cts", "typescript"],
  ["xml", "xml"], ["html", "xml"], ["xhtml", "xml"], ["svg", "xml"],
  ["yaml", "yaml"], ["yml", "yaml"],
]);

export const CODE_HIGHLIGHT_LIMIT = 32_000;
const maxLines = 1_000;
const maxNodes = 5_000;
const maxDepth = 64;
type HighlightNode = ReturnType<typeof highlighter.highlight>["children"][number];

function renderNodes(nodes: HighlightNode[], budget: { remaining: number }, depth = 0): ReactNode[] {
  if (depth > maxDepth) throw new Error("Code highlighting nesting limit");
  return nodes.map((node, key) => {
    if (--budget.remaining < 0) throw new Error("Code highlighting node limit");
    if (node.type === "text") return node.value;
    if (node.type !== "element") return null;
    const classes = node.properties.className;
    // Grammar output supplies only token classes; never forward tags, attributes or HTML.
    return <span key={key} className={Array.isArray(classes) ? classes.filter(value => typeof value === "string").join(" ") : undefined}>
      {renderNodes(node.children, budget, depth + 1)}
    </span>;
  });
}

/** Return highlighted React text/spans, or the exact source when highlighting is unavailable. */
export function highlightCode(text: string, language: string): ReactNode {
  const grammar = languages.get(language.trim().toLowerCase());
  if (!grammar || text.length > CODE_HIGHLIGHT_LIMIT || text.split(/\r\n|\r|\n/, maxLines + 1).length > maxLines) return text;
  try {
    return renderNodes(highlighter.highlight(grammar, text).children, { remaining: maxNodes });
  } catch {
    // Partial or pathological snippets must remain readable while a response streams.
    return text;
  }
}
