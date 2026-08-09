import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { IdentityAccess } from "@/components/identity-access";
import type { EffectiveIdentity } from "@/types";

function identity(role: "viewer" | "operator"): EffectiveIdentity {
  return {
    principal_id: "principal-opaque-1",
    tenant_id: "tenant-1",
    tenant_slug: "payments",
    token_role: role,
    mapped_role: role,
    effective_role: role,
    scopes: role === "operator" ? ["read", "realtime", "write"] : ["read", "realtime"],
    expires_at: 4_102_444_800,
  };
}

const baseProps = {
  open: true,
  onOpenChange: vi.fn(),
  authConfigured: true,
  authStatus: "public" as const,
  identity: null,
  onSignIn: vi.fn(async () => undefined),
  onSignOut: vi.fn(),
};

describe("identity access", () => {
  it("offers Hosted UI sign-in without collecting a passcode", () => {
    render(<IdentityAccess {...baseProps} />);

    expect(screen.getByRole("dialog", { name: "Sign in to Hindsight" })).toBeVisible();
    expect(screen.queryByLabelText(/passcode/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Sign in securely/i }));
    expect(baseProps.onSignIn).toHaveBeenCalledOnce();
  });

  it("shows the server-resolved viewer tenant and role", () => {
    render(
      <IdentityAccess
        {...baseProps}
        authStatus="authenticated"
        identity={identity("viewer")}
      />,
    );

    expect(screen.getByText("payments")).toBeVisible();
    expect(screen.getByText("viewer")).toBeVisible();
    expect(screen.getByText(/Effective scopes: read, realtime/)).toBeVisible();
  });

  it("lets an authenticated operator sign out", () => {
    const onSignOut = vi.fn();
    render(
      <IdentityAccess
        {...baseProps}
        authStatus="authenticated"
        identity={identity("operator")}
        onSignOut={onSignOut}
      />,
    );

    expect(screen.getByText("operator")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(onSignOut).toHaveBeenCalledOnce();
  });
});
