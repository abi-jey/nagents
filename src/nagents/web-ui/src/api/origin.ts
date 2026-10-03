/** App requests use ngn routes only; provider endpoints are server settings. */
export function apiPath(path: string): string {
  if (!path || /^(?:[a-z][a-z0-9+.-]*:|\/)/i.test(path) || /[\\\u0000-\u0020\u007f]/.test(path))
    throw new Error("Requests must use a local ngn API route.");
  return `/api/${path}`;
}

export function serverSocketUrl(path: string, pageUrl?: string): string {
  const location = globalThis.location;
  const current = location ? location.href || `${location.protocol}//${location.host}/` : "";
  const page = new URL(pageUrl || current);
  if (!["http:", "https:"].includes(page.protocol) || page.username || page.password ||
      (current && page.origin !== new URL(current).origin))
    throw new Error("WebSockets must connect to the ngn server origin.");
  if (!path.startsWith("/api/") || apiPath(path.slice(5)) !== path)
    throw new Error("WebSockets must use a local ngn API route.");
  const url = new URL(path, page);
  if (url.origin !== page.origin) throw new Error("WebSockets must connect to the ngn server origin.");
  url.protocol = page.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}
