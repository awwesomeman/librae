-- Bind newly registered live runs to a sanitized broker execution identity.
ALTER TABLE backtest_runs
    ADD COLUMN execution_identity JSONB;

ALTER TABLE backtest_runs
    ADD CONSTRAINT chk_execution_identity_object
    CHECK (execution_identity IS NULL OR jsonb_typeof(execution_identity) = 'object');

UPDATE librae_schema_revision
SET revision = 2, updated_at = NOW()
WHERE singleton = TRUE AND revision = 1;
