-- The inputs that determine a run's rows (data version, seed, clock, planned counts, chunk_rows, schema), so
-- resume can accept an edited-but-equivalent config and name exactly what differs. NULL for older runs.
ALTER TABLE {meta}.generation_runs ADD COLUMN resume_fields jsonb;
