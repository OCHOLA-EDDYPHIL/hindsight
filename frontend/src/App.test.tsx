import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "@/App";

const useCockpitMock = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/use-cockpit", () => ({ useCockpit: useCockpitMock }));

function cockpit(canWrite: boolean, loadState: "loading" | "ready" = "ready") {
  const noop = vi.fn();
  return {
    authConfigured: true,
    authStatus: "authenticated",
    identity: {
      principal_id: "principal-1",
      tenant_id: "tenant-1",
      tenant_slug: "payments",
      token_role: canWrite ? "operator" : "viewer",
      mapped_role: canWrite ? "operator" : "viewer",
      effective_role: canWrite ? "operator" : "viewer",
      scopes: canWrite ? ["read", "realtime", "write"] : ["read", "realtime"],
      expires_at: 4_102_444_800,
    },
    canWrite,
    connection: "live",
    loadState,
    loadError: "",
    scenario: null,
    namespace: "demo:payments",
    snapshot: null,
    incidents: [],
    incident: null,
    run: null,
    influence: [],
    notice: null,
    incidentInput: "processor timeout report",
    rewindAnchor: null,
    rewindTimestamp: "",
    rewindReason: "Remove stale guidance",
    rewindPreview: null,
    busy: null,
    retryInitialLoad: noop,
    signIn: vi.fn(async () => undefined),
    signOut: noop,
    selectIncident: noop,
    setIncidentInput: noop,
    resetDemo: noop,
    poisonDemo: noop,
    startRun: noop,
    decideRun: noop,
    setRewindTimestamp: noop,
    setRewindReason: noop,
    previewRewind: noop,
    executeRewind: noop,
    selectHistorical: noop,
  };
}

describe("product access rendering", () => {
  beforeEach(() => useCockpitMock.mockReset());

  it("does not render mutation controls for a viewer", () => {
    useCockpitMock.mockReturnValue(cockpit(false));
    render(<App />);

    expect(
      screen.queryByRole("region", { name: "Protected operator controls" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Analyze incident" })).not.toBeInTheDocument();
  });

  it("renders mutation controls only for effective write access", () => {
    useCockpitMock.mockReturnValue(cockpit(true));
    render(<App />);

    expect(screen.getByRole("region", { name: "Protected operator controls" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Analyze incident" })).toBeEnabled();
  });

  it("withholds mutation controls until the authenticated replay is ready", () => {
    useCockpitMock.mockReturnValue(cockpit(true, "loading"));
    const { rerender } = render(<App />);

    expect(
      screen.queryByRole("region", { name: "Protected operator controls" }),
    ).not.toBeInTheDocument();

    useCockpitMock.mockReturnValue(cockpit(true, "ready"));
    rerender(<App />);

    expect(screen.getByRole("region", { name: "Protected operator controls" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Analyze incident" })).toBeEnabled();
  });

  it("preserves an open walkthrough across ready replay refreshes", () => {
    useCockpitMock.mockReturnValue(cockpit(true, "ready"));
    const { rerender } = render(<App />);
    const toggle = screen.getByRole("button", { name: "Walkthrough" });

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("complementary")).toBeVisible();

    useCockpitMock.mockReturnValue({ ...cockpit(true, "ready"), connection: "reconnecting" });
    rerender(<App />);

    expect(screen.getByRole("button", { name: "Hide walkthrough" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByRole("complementary")).toBeVisible();
  });
});
