-- Bootstrap marker table. The real memory schema lands with issue #10;
-- this migration exists so the runner and smoke test have something to verify.
CREATE TABLE IF NOT EXISTS app_meta (
    key STRING PRIMARY KEY,
    value STRING NOT NULL
);

UPSERT INTO app_meta (key, value) VALUES ('schema_bootstrap', 'ok');
