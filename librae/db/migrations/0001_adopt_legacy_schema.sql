-- Adopt the structurally verified, formerly unversioned Librae schema.
-- The Python runner validates the legacy revision before executing this file,
-- and the caller owns one transaction plus the schema advisory lock.
CREATE TABLE IF NOT EXISTS librae_schema_revision (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO librae_schema_revision (singleton, revision)
VALUES (TRUE, 1)
ON CONFLICT (singleton) DO UPDATE
SET revision = EXCLUDED.revision, updated_at = NOW()
WHERE librae_schema_revision.revision = 0;

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'quant_app') THEN
        EXECUTE 'REVOKE INSERT, UPDATE, DELETE ON librae_schema_revision FROM quant_app';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        EXECUTE 'REVOKE INSERT, UPDATE, DELETE ON librae_schema_revision FROM grafana_reader';
    END IF;
END
$$;
