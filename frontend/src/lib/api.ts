import type { RuntimeConfig } from "@/types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export function apiUrl(config: RuntimeConfig, path: string): string {
  return `${config.apiBase || "/v1"}${path}`;
}

export async function requestJson<T>(
  config: RuntimeConfig,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(apiUrl(config, path), {
    credentials: "include",
    ...options,
    headers: {
      "content-type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string; error?: string };
      detail = body.detail || body.error || detail;
    } catch {
      // Proxy failures are not guaranteed to be JSON.
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function snapshotUrl(
  config: RuntimeConfig,
  namespace: string,
  asOf?: string | null,
): string {
  const url = config.snapshotBase
    ? new URL(config.snapshotBase, window.location.origin)
    : new URL(
        apiUrl(config, `/namespaces/${encodeURIComponent(namespace)}/beliefs`),
        window.location.origin,
      );
  if (config.snapshotBase) url.searchParams.set("namespace", namespace);
  if (asOf) url.searchParams.set("as_of", asOf);
  return url.toString();
}
