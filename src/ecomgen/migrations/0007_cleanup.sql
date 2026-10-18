-- `ecomgen cleanup-files`: ledger rows are never removed; a deleted file's row is marked with when and by which
-- cleanup. A cleanup row is inserted before any deletion and COMPLETED once its audit report is in place, so an
-- interrupted cleanup is visible and its report can be finished later. The report is built only from this row
-- (details: staging dirs, files already gone, files kept because they changed) and the marked ledger rows, so a
-- rebuilt report has the same bytes.
CREATE TABLE {meta}.cleanups (
    cleanup_id  text PRIMARY KEY CHECK (cleanup_id ~ '^cln_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}$'),
    target      jsonb NOT NULL CHECK (jsonb_typeof(target) = 'object'),
    token       char(12) NOT NULL CHECK (token ~ '^[0-9a-f]{12}$'),
    details     jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(details) = 'object'),
    state       text NOT NULL CHECK (state IN ('RUNNING', 'COMPLETED')),
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz CHECK (finished_at >= started_at),
    CHECK ((state = 'RUNNING') = (finished_at IS NULL))
);

ALTER TABLE {meta}.published_files
    ADD COLUMN deleted_at timestamptz,
    ADD COLUMN deleted_by text REFERENCES {meta}.cleanups (cleanup_id),
    ADD CONSTRAINT published_files_deleted_both CHECK ((deleted_at IS NULL) = (deleted_by IS NULL)),
    ADD CONSTRAINT published_files_manifest_kept CHECK (feed <> 'manifest' OR deleted_at IS NULL);

ALTER TABLE {meta}.file_deliveries
    ADD COLUMN deleted_at timestamptz,
    ADD COLUMN deleted_by text REFERENCES {meta}.cleanups (cleanup_id),
    ADD CONSTRAINT file_deliveries_deleted_both CHECK ((deleted_at IS NULL) = (deleted_by IS NULL)),
    ADD CONSTRAINT file_deliveries_manifest_kept CHECK (original_path IS NOT NULL OR deleted_at IS NULL);
