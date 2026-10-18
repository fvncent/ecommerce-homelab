-- Simulated deliveries of a run's published files (`ecomgen replay-files`). A replay is identified by a hash of its
-- run and parameters, so repeating the same command resumes or verifies it instead of delivering a second copy.
-- A file_deliveries row is inserted only after the delivered copy is durable in place; the delivery manifest's
-- row (original_path NULL) and replays.state = 'COMPLETED' commit together, last (enforced by the code).
CREATE TABLE {meta}.replays (
    replay_id   text PRIMARY KEY CHECK (replay_id ~ '^rpl_[0-9a-f]{12}$'),
    run_id      text NOT NULL REFERENCES {meta}.generation_runs (run_id),
    params      jsonb NOT NULL CHECK (jsonb_typeof(params) = 'object'),
    state       text NOT NULL CHECK (state IN ('RUNNING', 'COMPLETED')),
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz CHECK (finished_at >= started_at),
    CHECK ((state = 'RUNNING') = (finished_at IS NULL)),
    UNIQUE (replay_id, run_id)
);

-- Relative paths: not absolute, no empty/./.. components, no backslash.
CREATE TABLE {meta}.file_deliveries (
    replay_id      text NOT NULL,
    run_id         text NOT NULL,
    delivered_path text NOT NULL CHECK (delivered_path !~ '(^/|//|(^|/)\.\.?(/|$)|\\)'),
    original_path  text CHECK (original_path !~ '(^/|//|(^|/)\.\.?(/|$)|\\)'),  -- NULL: the delivery manifest
    sha256         char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    bytes          bigint NOT NULL CHECK (bytes >= 0),
    arrived_at     timestamptz,
    duplicate      boolean NOT NULL DEFAULT false,
    delivered_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (replay_id, delivered_path),
    FOREIGN KEY (replay_id, run_id) REFERENCES {meta}.replays (replay_id, run_id),
    FOREIGN KEY (run_id, original_path) REFERENCES {meta}.published_files (run_id, rel_path),  -- an original
    CHECK (starts_with(delivered_path, 'deliveries/replay_id=' || replay_id || '/')),
    -- the manifest row: no original, no arrival, not a duplicate, at the replay's manifest path; and vice versa
    CHECK ((original_path IS NULL) = (arrived_at IS NULL)),
    CHECK (original_path IS NOT NULL OR NOT duplicate),
    CHECK ((original_path IS NULL)
           = (delivered_path = 'deliveries/replay_id=' || replay_id || '/delivery_manifest.json'))
);
