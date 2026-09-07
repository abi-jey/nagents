export function CodeBlock({ label, text }: { label: string; text: string }) {
  return (
    <section className="code-block">
      <h3>{label}</h3>
      <pre tabIndex={0} role="region" aria-label={label}>
        <code>{text}</code>
      </pre>
    </section>
  );
}
