import type { RuntimeConfig } from "@/types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function joinApiPath(base: string, path: string): string {
  if (!path.startsWith("/")) throw new Error("API paths must start with a slash");
  return `${base.replace(/\/$/, "")}${path}`;
}

export function publicApiUrl(config: RuntimeConfig, path: string): string {
  return joinApiPath(config.publicApiBase || "/v1", path);
}

export function productApiUrl(config: RuntimeConfig, path: string): string {
  return joinApiPath(config.productApiBase || config.protectedApiBase || "/v2", path);
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string; error?: string };
      detail = body.detail || body.error || detail;
    } catch {
      // Proxy and edge failures are not guaranteed to be JSON.
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function requestHeaders(options: RequestInit): Headers {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  headers.set("accept", "application/json");
  return headers;
}

export async function requestPublicJson<T>(
  config: RuntimeConfig,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = requestHeaders(options);
  headers.delete("authorization");
  const response = await fetch(publicApiUrl(config, path), {
    ...options,
    credentials: "omit",
    headers,
  });
  return parseResponse<T>(response);
}

export async function requestProductJson<T>(
  config: RuntimeConfig,
  path: string,
  accessToken: string,
  options: RequestInit = {},
): Promise<T> {
  if (!accessToken.trim()) throw new ApiError("protected bearer credential required", 401);
  const headers = requestHeaders(options);
  headers.set("authorization", `Bearer ${accessToken}`);
  const response = await fetch(productApiUrl(config, path), {
    ...options,
    credentials: "omit",
    headers,
  });
  return parseResponse<T>(response);
}

export function publicSnapshotUrl(
  config: RuntimeConfig,
  namespace: string,
  asOf?: string | null,
): string {
  const url = config.snapshotBase
    ? new URL(config.snapshotBase, window.location.origin)
    : new URL(
        publicApiUrl(config, `/namespaces/${encodeURIComponent(namespace)}/beliefs`),
        window.location.origin,
      );
  if (config.snapshotBase) url.searchParams.set("namespace", namespace);
  if (asOf) url.searchParams.set("as_of", asOf);
  return url.toString();
}
