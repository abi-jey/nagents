export function CodeBlock({ label, text, language }: { label: string; text: string; language?: string }) {
  return (
    <section className="code-block" data-language={language || undefined}>
      <h3>{label}</h3>
      <pre tabIndex={0} role="region" aria-label={label}>
        <code>{text}</code>
      </pre>
    </section>
  );
}
