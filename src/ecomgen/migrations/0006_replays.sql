-- Simulated deliveries of a run's published files (`ecomgen replay-files`). A replay is identified by a hash of its
-- run and parameters, so repeating the same command resumes or verifies it instead of delivering a second copy.
-- A file_deliveries row is inserted only after the delivered copy is durable in place; the delivery manifest's
-- row (original_path NULL) goes last, and only then is the replay COMPLETED.
CREATE TABLE {meta}.replays (
    replay_id   text PRIMARY KEY CHECK (replay_id ~ '^rpl_[0-9a-f]{12}$'),
    run_id      text NOT NULL REFERENCES {meta}.generation_runs (run_id),
    params      jsonb NOT NULL CHECK (jsonb_typeof(params) = 'object'),
    state       text NOT NULL CHECK (state IN ('RUNNING', 'COMPLETED')),
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz CHECK (finished_at >= started_at),
    CHECK ((state = 'RUNNING') = (finished_at IS NULL))
);

CREATE TABLE {meta}.file_deliveries (
    replay_id      text NOT NULL REFERENCES {meta}.replays (replay_id),
    delivered_path text NOT NULL CHECK (delivered_path !~ '(^/|\.\.)'),
    original_path  text CHECK (original_path !~ '(^/|\.\.)'),  -- NULL: the delivery manifest itself
    sha256         char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    bytes          bigint NOT NULL CHECK (bytes >= 0),
    arrived_at     timestamptz,
    duplicate      boolean NOT NULL DEFAULT false,
    delivered_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (replay_id, delivered_path)
);
