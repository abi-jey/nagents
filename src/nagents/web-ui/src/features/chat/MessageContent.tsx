import { CodeBlock } from "../../components/CodeBlock.js";

export function MessageContent({ text }: { text: string }) {
  // Recognize fenced code only. Everything else remains literal, escaped text.
  const parts = text.split(/(^```[^\n]*(?:\n|$))/m);
  let code = false;
  let language = "";
  return (
    <>
      {parts.map((part, index) => {
        if (index % 2) {
          language = part.slice(3).trim();
          code = !code;
          return null;
        }
        if (!part) return null;
        return code ? (
          <CodeBlock
            key={index}
            label={language ? `Code (${language})` : "Code"}
            text={part}
          />
        ) : (
          <div key={index} className="entry-content">
            {part}
          </div>
        );
      })}
    </>
  );
}
