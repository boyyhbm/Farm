import type { Page } from "@/types";
let csrf = "";
export function setCsrf(value: string) {
  csrf = value;
}
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export async function api<T = Record<string, unknown>>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  const method = options.method || "GET";
  if (options.body && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json");
  if (method !== "GET") {
    headers.set("X-CSRF-Token", csrf);
    if (method === "POST" && !headers.has("Idempotency-Key"))
      headers.set("Idempotency-Key", crypto.randomUUID());
  }
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const data = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new ApiError(
      response.status,
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export async function download(path: string, name: string) {
  const r = await fetch(`/api/v1${path}`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(await r.text());
  const url = URL.createObjectURL(await r.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

export async function allPages(path: string): Promise<Page> {
  const separator = path.includes("?") ? "&" : "?";
  const first = await api<Page>(`${path}${separator}page=1&page_size=100`);
  const rows = [...first.items];
  for (let page = 2; rows.length < first.total; page++) {
    const next = await api<Page>(
      `${path}${separator}page=${page}&page_size=100`,
    );
    if (!next.items.length) break;
    rows.push(...next.items);
  }
  return { ...first, items: rows, page_size: Math.max(1, rows.length) };
}
