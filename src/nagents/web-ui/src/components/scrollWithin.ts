/** Reveal a transcript record without scrolling the application or document. */
export function scrollWithin(container: HTMLElement, target: HTMLElement,
  block: "start" | "nearest" = "nearest", behavior: ScrollBehavior = "auto") {
  if (!container.contains(target)) return;
  const frame = container.getBoundingClientRect();
  const record = target.getBoundingClientRect();
  const top = record.top - frame.top - container.clientTop;
  const bottom = record.bottom - frame.top - container.clientTop;
  let offset = 0;
  if (block === "start" || top < 0) offset = top;
  else if (bottom > container.clientHeight) offset = bottom - container.clientHeight;
  else return;
  const next = Math.max(0, Math.min(container.scrollHeight - container.clientHeight, container.scrollTop + offset));
  container.scrollTo({ top: next, behavior });
}
