export async function request(
  path: string,
  token = "",
  body?: object,
  signal?: AbortSignal,
): Promise<Response> {
  const response = await fetch(`/api/${path}`, {
    method: body ? "POST" : "GET",
    headers: {
      ...(token ? { "X-Ngn-Token": token } : {}),
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
    credentials: "same-origin",
    signal,
  });
  if (!response.ok) {
    const error = (await response.json().catch(() => ({}))) as {
      detail?: string;
    };
    throw new Error(
      error.detail || `Local request failed (${response.status}).`,
    );
  }
  return response;
}
