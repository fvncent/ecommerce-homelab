-- `ecomgen advance`: a schema set holds either one `generate` run (as_of NULL) or a chain of clock steps, each
-- moving the simulation clock from the previous step's as_of to its own. The chain's shape is enforced here: one
-- first step, every as_of used once, every previous_as_of is an existing step's as_of and used once, time only
-- moves forward. (generate refusing a schema set with any run, and advance refusing one with a generate run, keep
-- the two kinds apart; that rule stays in code, under the run lock.)
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

CREATE UNIQUE INDEX generation_runs_one_first_step ON {meta}.generation_runs ((true))
    WHERE as_of IS NOT NULL AND previous_as_of IS NULL;
