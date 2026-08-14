import { GithubLogo, ShieldCheck } from "@phosphor-icons/react";
import { lazy, Suspense, useState } from "react";

import {
  BeliefLedger,
  CausalRail,
  ConnectionState,
  EmptySurface,
  ErrorSurface,
  InfluenceLedger,
  LoadingSurface,
  OperationLedger,
  OutcomeComparison,
  StoryHeader,
  Timeline,
} from "@/components/cockpit";
import { IdentityAccess } from "@/components/identity-access";
import { useCockpit } from "@/hooks/use-cockpit";

const OperatorConsole = lazy(() =>
  import("@/components/operator-console").then((module) => ({
    default: module.OperatorConsole,
  })),
);

export default function App() {
  const cockpit = useCockpit();
  const [identityOpen, setIdentityOpen] = useState(false);
  const showReplay = cockpit.loadState === "ready";
  const brandHref = typeof window === "undefined"
    ? "/"
    : `${window.location.pathname}${window.location.search}`;

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to replay
      </a>
      <header className="site-header">
        <a className="brand" href={brandHref} aria-label="Hindsight governed memory replay">
          <span className="brand-mark" aria-hidden="true">H</span>
          <span>
            <strong>Hindsight</strong>
            <small>governed memory replay</small>
          </span>
        </a>
        <div className="header-actions">
          <ConnectionState state={cockpit.connection} />
          <IdentityAccess
            open={identityOpen}
            onOpenChange={setIdentityOpen}
            authConfigured={cockpit.authConfigured}
            authStatus={cockpit.authStatus}
            identity={cockpit.identity}
            onSignIn={cockpit.signIn}
            onSignOut={cockpit.signOut}
          />
        </div>
      </header>

      <main id="main">
        {cockpit.loadState === "loading" ? <LoadingSurface /> : null}
        {cockpit.loadState === "empty" ? (
          <EmptySurface onSignIn={() => setIdentityOpen(true)} />
        ) : null}
        {cockpit.loadState === "error" ? (
          <ErrorSurface message={cockpit.loadError} onRetry={cockpit.retryInitialLoad} />
        ) : null}
        {showReplay ? (
          <>
            <StoryHeader
              incident={cockpit.incident}
              namespace={cockpit.namespace}
              run={cockpit.run}
              scenario={cockpit.scenario}
            />
            <CausalRail
              scenario={cockpit.scenario}
              snapshot={cockpit.snapshot}
              activeRun={cockpit.run}
            />
            <OutcomeComparison scenario={cockpit.scenario} activeRun={cockpit.run} />
            <Timeline snapshot={cockpit.snapshot} onSelect={cockpit.selectHistorical} />
            <div className="evidence-grid">
              <BeliefLedger snapshot={cockpit.snapshot} />
              <InfluenceLedger
                influence={cockpit.influence}
                state={cockpit.influenceState}
                error={cockpit.influenceError}
              />
            </div>
            <OperationLedger operations={cockpit.snapshot?.operations || []} />
          </>
        ) : null}

        {cockpit.canWrite && showReplay ? (
          <Suspense fallback={<p className="phase-trace-unavailable">Loading controls…</p>}>
            <OperatorConsole
              incidents={cockpit.incidents}
              incident={cockpit.incident}
              run={cockpit.run}
              incidentInput={cockpit.incidentInput}
              busy={cockpit.busy}
              rewindAnchor={cockpit.rewindAnchor}
              scenario={cockpit.scenario}
              snapshot={cockpit.snapshot}
              rewindTimestamp={cockpit.rewindTimestamp}
              rewindReason={cockpit.rewindReason}
              rewindPreview={cockpit.rewindPreview}
              consolidationCandidates={cockpit.consolidationCandidates}
              consolidationPreview={cockpit.consolidationPreview}
              onIncident={cockpit.selectIncident}
              onIncidentInput={cockpit.setIncidentInput}
              onReset={cockpit.resetDemo}
              onPoison={cockpit.poisonDemo}
              onRun={cockpit.startRun}
              onDecision={cockpit.decideRun}
              onRewindTimestamp={cockpit.setRewindTimestamp}
              onRewindReason={cockpit.setRewindReason}
              onPreview={cockpit.previewRewind}
              onExecute={cockpit.executeRewind}
              onLoadCandidates={cockpit.loadConsolidationCandidates}
              onPreviewCandidateReview={cockpit.previewConsolidationReview}
              onExecuteCandidateReview={cockpit.executeConsolidationReview}
              onSignOut={cockpit.signOut}
            />
          </Suspense>
        ) : null}

        <div
          id="notice"
          className={cockpit.notice?.kind === "error" ? "notice error" : "notice"}
          role={cockpit.notice?.kind === "error" ? "alert" : "status"}
          aria-live="polite"
          hidden={!cockpit.notice}
        >
          {cockpit.notice?.message}
        </div>
      </main>

      <footer className="site-footer">
        <p>
          <ShieldCheck aria-hidden="true" size={16} />
          Every recommendation should be explainable and reversible.
        </p>
        <nav aria-label="Product links">
          <a href="/v1/docs">API</a>
          <a href="https://github.com/OCHOLA-EDDYPHIL/hindsight">
            <GithubLogo aria-hidden="true" size={15} />
            Source
          </a>
        </nav>
      </footer>
    </>
  );
}
