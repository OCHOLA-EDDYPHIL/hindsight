-- Preserve each authorized execution while allowing one outcome-free replacement.

DROP INDEX IF EXISTS learning_evidence_consumed_reset_idx;

CREATE UNIQUE INDEX IF NOT EXISTS learning_evidence_execution_study_idx
    ON learning_evidence_records (execution_authorization_id, evidence_kind)
    WHERE execution_authorization_id IS NOT NULL AND evidence_kind = 'study';

CREATE UNIQUE INDEX IF NOT EXISTS learning_evidence_scientific_reset_idx
    ON learning_evidence_records (protocol_authorization_id, evidence_kind)
    WHERE protocol_authorization_id IS NOT NULL
        AND evidence_kind = 'study'
        AND result IN ('accepted', 'not_demonstrated');
