import { memo, useMemo } from "react";
import { highlightCode } from "./codeHighlighting.js";

export interface CodeBlockProps {
  /** Visible heading and accessible name of the keyboard-scrollable code region. */
  label: string;
  /** Literal source, including all indentation and trailing whitespace. */
  text: string;
  /** Optional language name or alias; absent and unsupported names render as plain text. */
  language?: string;
}

/** Reusable code viewer. Load codeBlock.css once in the host application's entry point. */
export const CodeBlock = memo(function CodeBlock({ label, text, language = "" }: CodeBlockProps) {
  const content = useMemo(() => highlightCode(text, language), [text, language]);
  return (
    <section className="code-block" data-language={language || undefined}>
      <h3>{label}</h3>
      <pre tabIndex={0} role="region" aria-label={label}>
        <code>{content}</code>
      </pre>
    </section>
  );
});
