export class RequestError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "RequestError";
  }
}

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
  return checked(response);
}

export async function requestBinary(
  path: string,
  token: string,
  body: Blob,
  headers: Record<string, string>,
  signal: AbortSignal,
): Promise<Response> {
  return checked(await fetch(`/api/${path}`, {
    method: "POST",
    headers: { ...headers, "X-Ngn-Token": token, "Content-Type": body.type },
    body,
    cache: "no-store",
    credentials: "same-origin",
    signal,
  }));
}

async function checked(response: Response): Promise<Response> {
  if (!response.ok) {
    const error = (await response.json().catch(() => ({}))) as {
      detail?: string;
    };
    throw new RequestError(
      error.detail || `Local request failed (${response.status}).`,
      response.status,
    );
  }
  return response;
}
