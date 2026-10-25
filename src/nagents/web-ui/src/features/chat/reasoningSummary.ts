/** A provider summary needs readable content, beyond empty Markdown markers. */
export function hasReasoningText(value: string): boolean {
  return /[^\s#*_`~>\-\p{Cf}\p{Cc}\p{M}]/u.test(value);
}

/** Separate a provider's opening Markdown heading from its summary body. */
export function reasoningParts(value: string): { title: string; body: string } {
  const [first = "", ...rest] = value.trim().split("\n");
  if (/^(\*\*.+\*\*|__.+__|#{1,6}\s+.+)$/.test(first))
    return { title: first.replace(/^(?:\*\*|__|#{1,6}\s+)|(?:\*\*|__)$/g, ""), body: rest.join("\n").trim() };
  return { title: "Reasoning", body: value.trim() };
}

export function reasoningExcerpt(body: string, title: string): string {
  return body.split(/(?<=[.!?])\s/)[0]?.replace(/\*\*|__/g, "") || title;
}
