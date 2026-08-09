import type { HostedUiAuthConfig } from "@/types";

export const PKCE_TRANSACTION_KEY = "hindsight.oauth.pkce";
export const PKCE_TRANSACTION_TTL_MS = 10 * 60 * 1000;

const OAUTH_CALLBACK_PARAMS = [
  "code",
  "state",
  "error",
  "error_description",
  "error_uri",
] as const;

export interface AuthSession {
  accessToken: string;
  expiresAt: number;
}

export interface AuthAdapter {
  initialize(): Promise<AuthSession | null>;
  accessToken(): string | null;
  signIn(returnTo?: string): Promise<void>;
  signOut(): void;
  clear(): void;
}

export class AuthError extends Error {}

interface PkceTransaction {
  state: string;
  verifier: string;
  created_at: number;
  return_to: string;
}

interface BrowserAuthRuntime {
  crypto: Crypto;
  storage: Storage;
  fetch: typeof fetch;
  now: () => number;
  currentUrl: () => URL;
  navigate: (url: string) => void;
  replaceUrl: (url: string) => void;
}

function defaultRuntime(): BrowserAuthRuntime {
  return {
    crypto: window.crypto,
    storage: window.sessionStorage,
    fetch: window.fetch.bind(window),
    now: () => Date.now(),
    currentUrl: () => new URL(window.location.href),
    navigate: (url) => window.location.assign(url),
    replaceUrl: (url) => window.history.replaceState(null, "", url),
  };
}

function validatedUrl(value: string, label: string, hostedUi = false): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new AuthError(`${label} must be an absolute URL`);
  }
  const localDevelopment = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (
    (url.protocol !== "https:" && !(url.protocol === "http:" && localDevelopment)) ||
    url.username ||
    url.password
  ) {
    throw new AuthError(`${label} must use HTTPS`);
  }
  if (hostedUi && (url.search || url.hash)) {
    throw new AuthError(`${label} cannot contain a query or fragment`);
  }
  return url;
}

function validateConfig(config: HostedUiAuthConfig) {
  const hostedUi = validatedUrl(config.hostedUiBaseUrl, "Hosted UI URL", true);
  const redirect = validatedUrl(config.redirectUri, "OAuth redirect URI");
  const logout = validatedUrl(config.logoutUri, "OAuth logout URI");
  if (!config.clientId.trim()) throw new AuthError("OAuth client ID is required");
  const scopes = [...new Set(config.scopes.map((scope) => scope.trim()).filter(Boolean))];
  if (!scopes.length || scopes.some((scope) => !/^[A-Za-z0-9._:/-]+$/.test(scope))) {
    throw new AuthError("At least one valid OAuth scope is required");
  }
  return { hostedUi, redirect, logout, clientId: config.clientId.trim(), scopes };
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomValue(cryptoProvider: Crypto, bytes: number): string {
  return base64Url(cryptoProvider.getRandomValues(new Uint8Array(bytes)));
}

async function pkceChallenge(cryptoProvider: Crypto, verifier: string): Promise<string> {
  const digest = await cryptoProvider.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier),
  );
  return base64Url(new Uint8Array(digest));
}

function safeReturnTo(value: string | undefined, current: URL): string {
  if (!value) return `${current.pathname}${current.search}${current.hash}`;
  try {
    const target = new URL(value, current.origin);
    if (target.origin !== current.origin) throw new Error("cross-origin return URL");
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return `${current.pathname}${current.search}${current.hash}`;
  }
}

function parseTransaction(value: string | null): PkceTransaction | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Partial<PkceTransaction>;
    if (
      typeof parsed.state !== "string" ||
      !/^[A-Za-z0-9_-]{32,256}$/.test(parsed.state) ||
      typeof parsed.verifier !== "string" ||
      !/^[A-Za-z0-9_-]{43,128}$/.test(parsed.verifier) ||
      typeof parsed.created_at !== "number" ||
      !Number.isFinite(parsed.created_at) ||
      typeof parsed.return_to !== "string" ||
      parsed.return_to.length > 4096
    ) {
      return null;
    }
    return parsed as PkceTransaction;
  } catch {
    return null;
  }
}

function callbackPresent(url: URL): boolean {
  return OAUTH_CALLBACK_PARAMS.some((name) => url.searchParams.has(name));
}

export class HostedUiAuthAdapter implements AuthAdapter {
  private readonly config: ReturnType<typeof validateConfig>;
  private readonly runtime: BrowserAuthRuntime;
  private session: AuthSession | null = null;

  constructor(config: HostedUiAuthConfig, runtime: BrowserAuthRuntime = defaultRuntime()) {
    this.config = validateConfig(config);
    this.runtime = runtime;
  }

  accessToken(): string | null {
    if (this.session && this.session.expiresAt > this.runtime.now()) {
      return this.session.accessToken;
    }
    this.session = null;
    return null;
  }

  async initialize(): Promise<AuthSession | null> {
    const current = this.runtime.currentUrl();
    if (!callbackPresent(current)) {
      return this.accessToken() ? this.session : null;
    }

    const transaction = parseTransaction(this.runtime.storage.getItem(PKCE_TRANSACTION_KEY));
    this.runtime.storage.removeItem(PKCE_TRANSACTION_KEY);
    const callbackState = current.searchParams.get("state");
    const callbackCode = current.searchParams.get("code");
    const returnTo = transaction
      ? safeReturnTo(transaction.return_to, current)
      : undefined;
    const expired =
      !transaction ||
      transaction.created_at > this.runtime.now() ||
      this.runtime.now() - transaction.created_at > PKCE_TRANSACTION_TTL_MS;
    if (
      current.searchParams.has("error") ||
      !callbackCode ||
      !callbackState ||
      expired ||
      callbackState !== transaction?.state
    ) {
      this.stripCallback(current, returnTo);
      throw new AuthError("Sign-in response was invalid or expired. Please sign in again.");
    }

    const body = new URLSearchParams({
      grant_type: "authorization_code",
      client_id: this.config.clientId,
      code: callbackCode,
      redirect_uri: this.config.redirect.toString(),
      code_verifier: transaction.verifier,
    });
    let response: Response;
    try {
      response = await this.runtime.fetch(
        new URL("oauth2/token", `${this.config.hostedUi.toString().replace(/\/$/, "")}/`),
        {
          method: "POST",
          credentials: "omit",
          headers: {
            accept: "application/json",
            "content-type": "application/x-www-form-urlencoded",
          },
          body,
        },
      );
    } catch (error) {
      this.stripCallback(current, returnTo);
      throw new AuthError(`Sign-in token exchange failed: ${(error as Error).message}`);
    }
    if (!response.ok) {
      this.stripCallback(current, returnTo);
      throw new AuthError("Sign-in token exchange was rejected. Please sign in again.");
    }
    let payload: {
      access_token?: unknown;
      expires_in?: unknown;
      token_type?: unknown;
    };
    try {
      payload = (await response.json()) as typeof payload;
    } catch {
      this.stripCallback(current, returnTo);
      throw new AuthError("Sign-in token response was invalid. Please sign in again.");
    }
    const expiresIn = Number(payload.expires_in);
    if (
      typeof payload.access_token !== "string" ||
      !payload.access_token.trim() ||
      typeof payload.token_type !== "string" ||
      payload.token_type.toLowerCase() !== "bearer" ||
      !Number.isFinite(expiresIn) ||
      expiresIn <= 0 ||
      expiresIn > 86_400
    ) {
      this.stripCallback(current, returnTo);
      throw new AuthError("Sign-in token response was invalid. Please sign in again.");
    }
    this.session = {
      accessToken: payload.access_token,
      expiresAt: this.runtime.now() + expiresIn * 1000,
    };
    this.stripCallback(current, returnTo);
    return this.session;
  }

  async signIn(returnTo?: string): Promise<void> {
    const current = this.runtime.currentUrl();
    const verifier = randomValue(this.runtime.crypto, 64);
    const transaction: PkceTransaction = {
      state: randomValue(this.runtime.crypto, 32),
      verifier,
      created_at: this.runtime.now(),
      return_to: safeReturnTo(returnTo, current),
    };
    this.runtime.storage.setItem(PKCE_TRANSACTION_KEY, JSON.stringify(transaction));
    let challenge: string;
    try {
      challenge = await pkceChallenge(this.runtime.crypto, verifier);
    } catch {
      this.runtime.storage.removeItem(PKCE_TRANSACTION_KEY);
      throw new AuthError("This browser cannot create a secure PKCE challenge.");
    }
    const authorization = new URL(
      "oauth2/authorize",
      `${this.config.hostedUi.toString().replace(/\/$/, "")}/`,
    );
    authorization.search = new URLSearchParams({
      response_type: "code",
      client_id: this.config.clientId,
      redirect_uri: this.config.redirect.toString(),
      scope: this.config.scopes.join(" "),
      state: transaction.state,
      code_challenge: challenge,
      code_challenge_method: "S256",
    }).toString();
    this.runtime.navigate(authorization.toString());
  }

  signOut(): void {
    this.clear();
    const logout = new URL(
      "logout",
      `${this.config.hostedUi.toString().replace(/\/$/, "")}/`,
    );
    logout.search = new URLSearchParams({
      client_id: this.config.clientId,
      logout_uri: this.config.logout.toString(),
    }).toString();
    this.runtime.navigate(logout.toString());
  }

  clear(): void {
    this.session = null;
    this.runtime.storage.removeItem(PKCE_TRANSACTION_KEY);
  }

  private stripCallback(current: URL, returnTo?: string) {
    if (returnTo) {
      this.runtime.replaceUrl(returnTo);
      return;
    }
    for (const name of OAUTH_CALLBACK_PARAMS) current.searchParams.delete(name);
    this.runtime.replaceUrl(`${current.pathname}${current.search}${current.hash}`);
  }
}

export function createHostedUiAuthAdapter(
  config: HostedUiAuthConfig | null | undefined,
): AuthAdapter | null {
  return config ? new HostedUiAuthAdapter(config) : null;
}
