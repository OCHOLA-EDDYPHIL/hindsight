import { useState } from "react";

import { Button } from "@/components/ui/button";
import { formatTime } from "@/lib/format";
import type { ConsolidationCandidate, ConsolidationReviewPreview } from "@/types";

export default function LessonCandidateConsole({
  busy,
  candidates,
  preview,
  onLoad,
  onPreview,
  onExecute,
}: {
  busy: string | null;
  candidates: ConsolidationCandidate[];
  preview: ConsolidationReviewPreview | null;
  onLoad: () => void;
  onPreview: (candidateId: string, action: "approve" | "reject", reason: string) => void;
  onExecute: () => void;
}) {
  const [reason, setReason] = useState(
    "Reviewed against the cited incident evidence and operational safety constraints",
  );

  return (
    <div className="consolidation-review-console">
      <header>
        <div>
          <p className="section-kicker">Generated guidance</p>
          <h3>Lesson candidates</h3>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="compact"
          disabled={busy === "load-candidates"}
          onClick={onLoad}
        >
          {busy === "load-candidates" ? "Loading" : "Load pending"}
        </Button>
      </header>
      <div className="field">
        <label htmlFor="candidateReviewReason">Review reason</label>
        <input
          id="candidateReviewReason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </div>
      {candidates.length ? (
        <ul className="consolidation-candidates" aria-label="Pending lesson candidates">
          {candidates.map((candidate) => (
            <li key={candidate.candidate_id}>
              <div>
                <strong>{candidate.incident_title}</strong>
                <span>{candidate.content}</span>
                <small>
                  Candidate {candidate.candidate_fingerprint.slice(0, 12)} · evidence{" "}
                  {candidate.evidence_fingerprint.slice(0, 12)}
                </small>
                <ul className="candidate-evidence" aria-label="Candidate evidence">
                  {candidate.evidence.map((item) => (
                    <li key={item.evidence_id}>
                      <span>{item.relationship}</span>
                      <span>{item.content || "Evidence unavailable"}</span>
                      <small>
                        {item.matches_manifest && item.current
                          ? `Verified ${item.sha256.slice(0, 12)}`
                          : "Evidence changed or unavailable"}
                      </small>
                    </li>
                  ))}
                </ul>
              </div>
              <div className="candidate-review-actions">
                <Button
                  type="button"
                  variant="danger"
                  size="compact"
                  disabled={busy === "candidate-preview"}
                  onClick={() => onPreview(candidate.candidate_id, "reject", reason)}
                >
                  Preview rejection
                </Button>
                <Button
                  type="button"
                  variant="primary"
                  size="compact"
                  disabled={busy === "candidate-preview"}
                  onClick={() => onPreview(candidate.candidate_id, "approve", reason)}
                >
                  Preview approval
                </Button>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="phase-trace-unavailable">
          Load the queue to inspect pending candidates. Generated lessons are not recalled before
          approval.
        </p>
      )}
      {preview ? (
        <div className="candidate-review-preview" role="status" aria-live="polite">
          <span>
            {preview.request_payload.action === "approve"
              ? "Approval will create a new active successor."
              : "Rejection will retain the candidate as audit-only history."}
          </span>
          <small>
            Preview {preview.fingerprint.slice(0, 12)} expires {formatTime(preview.expires_at)}.
          </small>
          <Button
            type="button"
            variant={preview.request_payload.action === "approve" ? "primary" : "danger"}
            disabled={busy === "candidate-execute"}
            onClick={onExecute}
          >
            {busy === "candidate-execute" ? "Executing" : "Execute bound review"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
