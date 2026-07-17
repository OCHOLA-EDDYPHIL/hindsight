import { GithubLogo, ShieldCheck } from "@phosphor-icons/react";
import { useState } from "react";

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
import { OperatorAccess, OperatorConsole } from "@/components/operator-console";
import { useCockpit } from "@/hooks/use-cockpit";

export default function App() {
  const cockpit = useCockpit();
  const [operatorOpen, setOperatorOpen] = useState(false);
  const showReplay = cockpit.loadState === "ready";

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to replay
      </a>
      <header className="site-header">
        <a className="brand" href="/" aria-label="Hindsight governed memory replay">
          <span className="brand-mark" aria-hidden="true">H</span>
          <span>
            <strong>Hindsight</strong>
            <small>governed memory replay</small>
          </span>
        </a>
        <div className="header-actions">
          <ConnectionState state={cockpit.connection} />
          <OperatorAccess
            open={operatorOpen}
            onOpenChange={setOperatorOpen}
            operator={cockpit.operator}
            onUnlock={cockpit.unlockOperator}
          />
        </div>
      </header>

      <main id="main">
        {cockpit.loadState === "loading" ? <LoadingSurface /> : null}
        {cockpit.loadState === "empty" ? (
          <EmptySurface onOperator={() => setOperatorOpen(true)} />
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
            />
            <CausalRail scenario={cockpit.scenario} />
            <OutcomeComparison scenario={cockpit.scenario} activeRun={cockpit.run} />
            <Timeline snapshot={cockpit.snapshot} onSelect={cockpit.selectHistorical} />
            <div className="evidence-grid">
              <BeliefLedger snapshot={cockpit.snapshot} />
              <InfluenceLedger influence={cockpit.influence} />
            </div>
            <OperationLedger operations={cockpit.snapshot?.operations || []} />
          </>
        ) : null}

        <OperatorConsole
          operator={cockpit.operator}
          incidents={cockpit.incidents}
          incident={cockpit.incident}
          run={cockpit.run}
          incidentInput={cockpit.incidentInput}
          busy={cockpit.busy}
          rewindTimestamp={cockpit.rewindTimestamp}
          rewindReason={cockpit.rewindReason}
          rewindPreview={cockpit.rewindPreview}
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
          onLock={cockpit.lockOperator}
        />

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
