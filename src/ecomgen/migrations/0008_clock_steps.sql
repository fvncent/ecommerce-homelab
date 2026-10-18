-- `ecomgen advance`: a schema set holds either one `generate` run (as_of NULL) or a chain of clock steps, each
-- moving the simulation clock from the previous step's as_of to its own. Enforced here: every as_of used once,
-- every previous_as_of is an existing step's as_of and used once, time only moves forward, and exactly one run
-- without a previous_as_of: the generate run or the chain's first step, so the two kinds can't share a schema set
-- (both would write the same deterministic IDs).
ALTER TABLE {meta}.generation_runs
    ADD COLUMN as_of timestamptz,
    ADD COLUMN previous_as_of timestamptz,
    ADD CONSTRAINT generation_runs_as_of_key UNIQUE (as_of),
    ADD CONSTRAINT generation_runs_previous_as_of_key UNIQUE (previous_as_of),
    ADD CONSTRAINT generation_runs_clock_forward CHECK (as_of > previous_as_of),
    ADD CONSTRAINT generation_runs_step_has_as_of CHECK (previous_as_of IS NULL OR as_of IS NOT NULL);

ALTER TABLE {meta}.generation_runs
    ADD CONSTRAINT generation_runs_previous_step FOREIGN KEY (previous_as_of)
        REFERENCES {meta}.generation_runs (as_of);

CREATE UNIQUE INDEX generation_runs_one_origin ON {meta}.generation_runs ((true)) WHERE previous_as_of IS NULL;
