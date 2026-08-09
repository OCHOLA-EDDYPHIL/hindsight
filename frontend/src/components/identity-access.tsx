import { ShieldCheck, SignIn, SignOut, UserCircle } from "@phosphor-icons/react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import type { AuthStatus, EffectiveIdentity } from "@/types";

export function IdentityAccess({
  open,
  onOpenChange,
  authConfigured,
  authStatus,
  identity,
  onSignIn,
  onSignOut,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  authConfigured: boolean;
  authStatus: AuthStatus;
  identity: EffectiveIdentity | null;
  onSignIn: () => Promise<void>;
  onSignOut: () => void;
}) {
  const authenticated = authStatus === "authenticated" && identity !== null;
  const label = authenticated
    ? identity.effective_role === "operator"
      ? "Operator"
      : "Viewer"
    : authStatus === "initializing"
      ? "Checking sign-in"
      : "Sign in";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button
          id="identityButton"
          type="button"
          variant={authenticated ? "primary" : "quiet"}
          disabled={authStatus === "initializing"}
        >
          {authenticated ? (
            <UserCircle aria-hidden="true" size={16} weight="fill" />
          ) : (
            <SignIn aria-hidden="true" size={15} weight="bold" />
          )}
          <span id="identityLabel">{label}</span>
        </Button>
      </DialogTrigger>
      <DialogContent id="identityPanel" aria-label={authenticated ? "Product identity" : "Sign in"}>
        {authenticated ? (
          <>
            <DialogTitle className="text-xl font-semibold text-text">Product identity</DialogTitle>
            <DialogDescription className="mt-2 max-w-[52ch] text-sm leading-6 text-muted">
              Permissions come from the server-resolved tenant mapping and the verified access token.
            </DialogDescription>
            <dl className="identity-facts">
              <div>
                <dt>Tenant</dt>
                <dd>{identity.tenant_slug}</dd>
              </div>
              <div>
                <dt>Effective role</dt>
                <dd>{identity.effective_role}</dd>
              </div>
            </dl>
            <p className="identity-scope">
              <ShieldCheck aria-hidden="true" size={16} />
              Effective scopes: {identity.scopes.join(", ")}
            </p>
            <Button
              id="identitySignOut"
              className="mt-6 w-full"
              type="button"
              variant="quiet"
              onClick={onSignOut}
            >
              <SignOut aria-hidden="true" size={16} />
              Sign out
            </Button>
          </>
        ) : (
          <>
            <DialogTitle className="text-xl font-semibold text-text">Sign in to Hindsight</DialogTitle>
            <DialogDescription className="mt-2 max-w-[52ch] text-sm leading-6 text-muted">
              The public replay stays credential free. Hosted sign-in unlocks your tenant-scoped
              product view; mutation controls appear only when the server grants write scope.
            </DialogDescription>
            {!authConfigured ? (
              <p className="mt-4 text-sm leading-6 text-muted" role="status">
                Hosted sign-in is not configured for this deployment.
              </p>
            ) : null}
            <Button
              id="identitySignIn"
              className="mt-6 w-full"
              type="button"
              variant="primary"
              disabled={!authConfigured}
              onClick={() => void onSignIn()}
            >
              <SignIn aria-hidden="true" size={16} weight="bold" />
              Sign in securely
            </Button>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
