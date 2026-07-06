let apiKey = "";
let version = "";

export async function initClient(): Promise<void> {
  const resp = await fetch("/initialize.json");
  if (!resp.ok) throw new Error("Cannot reach mangarr backend");
  const data = await resp.json();
  apiKey = data.apiKey;
  version = data.version;
}

export function appVersion(): string {
  return version;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const resp = await fetch(`/api/v1${path}`, {
    method,
    headers: {
      "X-Api-Key": apiKey,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const data = await resp.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* not json */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};
