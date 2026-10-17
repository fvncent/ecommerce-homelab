-- resume_fields now records the generator World (seed, clock, counts) plus data_version, chunk_rows and the target
-- schema; resume rebuilds the World from it. Enforce the shape here, not only in Python.
ALTER TABLE {meta}.generation_runs ADD CONSTRAINT generation_runs_resume_fields_shape
    CHECK (resume_fields IS NULL OR (jsonb_typeof(resume_fields) = 'object'
           AND jsonb_typeof(resume_fields->'world') = 'object'));
