import type { ReactNode } from "react";

function href(value: string): string {
  const raw = value.startsWith("<") && value.endsWith(">") ? value.slice(1, -1) : value;
  const unescaped = raw.replace(/\\([\\()])/g, "$1");
  if (/[\s\u0000-\u001f\u007f]/.test(unescaped)) return "";
  try {
    const url = new URL(unescaped);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}

/** Small safe link renderer. Never interprets HTML or executable URL schemes. */
export function TextLinks({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  let literal = 0;
  for (let index = 0; index < text.length; index++) {
    if (text[index] === "\\") { index++; continue; }
    // Keep code spans literal, including Markdown examples inside them.
    if (text[index] === "`") {
      let end = index + 1;
      while (text[end] === "`") end++;
      const match = text.indexOf(text.slice(index, end), end);
      index = match < 0 ? text.length : match + end - index - 1;
      continue;
    }
    if (text[index] !== "[" || text[index - 1] === "!") continue;
    let labelEnd = index + 1;
    while (labelEnd < text.length && !["]", "[", "\n"].includes(text[labelEnd])) {
      if (text[labelEnd] === "\\") labelEnd++;
      labelEnd++;
    }
    if (text[labelEnd] !== "]" || text[labelEnd + 1] !== "(" || labelEnd === index + 1) { index = labelEnd - 1; continue; }
    let end = labelEnd + 2, depth = 1;
    for (; end < text.length && text[end] !== "\n"; end++) {
      if (text[end] === "\\") { end++; continue; }
      if (text[end] === "(") depth++;
      if (text[end] === ")" && --depth === 0) break;
    }
    if (depth || text[end] !== ")") { index = end - 1; continue; }
    const target = href(text.slice(labelEnd + 2, end));
    if (!target) { index = end; continue; }
    if (literal < index) parts.push(text.slice(literal, index));
    parts.push(<a key={index} href={target} target="_blank" rel="noopener noreferrer">{text.slice(index + 1, labelEnd).replace(/\\([\\\[\]])/g, "$1")}</a>);
    literal = end + 1; index = end;
  }
  if (literal < text.length) parts.push(text.slice(literal));
  return <>{parts}</>;
}
