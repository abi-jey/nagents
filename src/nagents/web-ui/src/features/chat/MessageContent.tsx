import { CodeBlock } from "../../components/CodeBlock.js";
import { TextLinks } from "../../components/TextLinks.js";

export function MessageContent({ text }: { text: string }) {
  // Code stays literal. Prose recognizes safe source links without parsing HTML.
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
            <TextLinks text={part} />
          </div>
        );
      })}
    </>
  );
}
