// Navigate the document, not the API fetch: the OIDC handshake needs a
// top-level navigation and must start on the hostname that owned the session.
let leaving: Promise<never> | undefined;

export async function sessionFetch(path: string, init: RequestInit): Promise<Response> {
  const response = await fetch(path, init);
  if (response.status !== 401 || typeof window === "undefined" ||
      !response.headers.get("content-type")?.includes("application/json")) return response;

  // Provider and integration errors are not workspace session expiry. Match
  // the existing OIDC middleware response, and leave other errors to the caller.
  const body: unknown = await response.clone().json().catch(() => null);
  if (!body || typeof body !== "object" || !("detail" in body) || body.detail !== "Sign in to NGN.") return response;
  if (!leaving) {
    window.location.replace(window.location.pathname === "/dashboard" ? "/dashboard" : "/");
    // The document is leaving. Concurrent requests must neither redirect again
    // nor paint a feature-specific error. Never replay an interrupted mutation.
    leaving = new Promise<never>(() => {});
  }
  return leaving;
}
