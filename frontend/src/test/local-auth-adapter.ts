import type { AuthAdapter, AuthSession } from "@/lib/auth";

/** Deterministic test seam. Production code never imports this adapter. */
export class LocalAuthAdapter implements AuthAdapter {
  readonly signInCalls: Array<string | undefined> = [];
  signOutCalls = 0;

  constructor(private session: AuthSession | null = null) {}

  initialize(): Promise<AuthSession | null> {
    return Promise.resolve(this.session);
  }

  accessToken(): string | null {
    if (!this.session || this.session.expiresAt <= Date.now()) {
      this.session = null;
      return null;
    }
    return this.session.accessToken;
  }

  signIn(returnTo?: string): Promise<void> {
    this.signInCalls.push(returnTo);
    return Promise.resolve();
  }

  signOut(): void {
    this.signOutCalls += 1;
    this.clear();
  }

  clear(): void {
    this.session = null;
  }
}
