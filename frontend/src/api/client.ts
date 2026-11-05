let apiKey = "";
let version = "";
let authMode: "managed" | "environment" | "external" = "external";

export async function initClient(): Promise<void> {
  const resp = await fetch("/initialize.json", { cache: "no-store" });
  if (!resp.ok) {
    let message = `Cannot initialize Mangarr (HTTP ${resp.status})`;
    try {
      const data = await resp.json();
      if (typeof data.detail === "string") message = data.detail;
    } catch { /* response may come from a reverse proxy */ }
    throw new Error(message);
  }
  const data = await resp.json();
  apiKey = data.apiKey;
  authMode = data.authMode ?? "external";
  version = data.version;
}

export function appVersion(): string {
  return version;
}

export function browserAuthMode() {
  return authMode;
}

export async function updateBrowserLogin(action: "password" | "logout", fields?: URLSearchParams) {
  const response = await fetch(`/auth/${action}`, { method: "POST", body: fields });
  if (response.ok || response.status === 401) {
    window.location.assign(response.ok && action === "password" ? "/login?changed=1" : "/login");
    return;
  }
  const data = await response.json().catch(() => ({}));
  throw new Error(data.detail ?? "Could not update the browser login.");
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
      ...(apiKey ? { "X-Api-Key": apiKey } : {}),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    if (resp.status === 401 && authMode === "managed") window.location.assign("/login");
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
