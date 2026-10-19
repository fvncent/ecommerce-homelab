-- Transactional outbox (Phase 3C): one append-only row per order / payment state transition, inserted in the same
-- transaction as the row change it describes. Order within an aggregate: aggregate_seq (1 = creation); overall:
-- (occurred_at, event_id). The id, the aggregate, the event type per sequence number and the state machine of
-- generate._lifecycle are all enforced here, not only in Python.
CREATE TABLE {integration}.outbox_events (
    event_id       text PRIMARY KEY,
    aggregate_type text NOT NULL CHECK (aggregate_type IN ('order', 'payment')),
    aggregate_id   text NOT NULL,
    aggregate_seq  integer NOT NULL CHECK (aggregate_seq >= 1),
    event_type     text NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version >= 1),
    occurred_at    timestamptz NOT NULL,
    order_id       text NOT NULL REFERENCES {app}.orders (order_id),
    payment_id     text REFERENCES {app}.payment_attempts (payment_id),
    payload        jsonb NOT NULL,
    run_id         text NOT NULL REFERENCES {meta}.generation_runs (run_id),
    UNIQUE (aggregate_id, aggregate_seq),
    CHECK (event_id = 'obx_' || aggregate_id || '_' || aggregate_seq),
    CHECK ((aggregate_type = 'payment') = (payment_id IS NOT NULL)),
    CHECK (aggregate_id = coalesce(payment_id, order_id)),
    CHECK ((aggregate_seq = 1) = (event_type IN ('OrderPlaced', 'PaymentAttemptCreated'))),
    CHECK (CASE event_type
        WHEN 'OrderPlaced' THEN aggregate_type = 'order'
            AND payload -> 'from' = 'null'::jsonb AND payload ->> 'to' = 'CREATED'
        WHEN 'OrderStatusChanged' THEN aggregate_type = 'order'
            AND (payload ->> 'from', payload ->> 'to') IN (('CREATED', 'CONFIRMED'), ('CREATED', 'CANCELLED'),
                ('CONFIRMED', 'CANCELLED'), ('CONFIRMED', 'PROCESSING'), ('PROCESSING', 'SHIPPED'),
                ('SHIPPED', 'DELIVERED'))
        WHEN 'PaymentAttemptCreated' THEN aggregate_type = 'payment'
            AND payload -> 'from' = 'null'::jsonb AND payload ->> 'to' = 'INITIATED'
        WHEN 'PaymentStateChanged' THEN aggregate_type = 'payment'
            AND (payload ->> 'from', payload ->> 'to') IN (('INITIATED', 'FAILED'), ('INITIATED', 'AUTHORIZED'),
                ('AUTHORIZED', 'VOIDED'), ('AUTHORIZED', 'CAPTURED'), ('CAPTURED', 'SETTLED'))
        ELSE false END)
);
CREATE INDEX outbox_events_occurred ON {integration}.outbox_events (occurred_at, event_id);
CREATE INDEX outbox_events_run ON {integration}.outbox_events (run_id);
CREATE INDEX outbox_events_order ON {integration}.outbox_events (order_id);

-- Append-only: an event, once committed, is never changed or removed.
CREATE FUNCTION {integration}.outbox_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'outbox_events is append-only (% refused)', TG_OP;
END $$;
CREATE TRIGGER outbox_events_no_update_delete BEFORE UPDATE OR DELETE ON {integration}.outbox_events
    FOR EACH ROW EXECUTE FUNCTION {integration}.outbox_events_append_only();
CREATE TRIGGER outbox_events_no_truncate BEFORE TRUNCATE ON {integration}.outbox_events
    FOR EACH STATEMENT EXECUTE FUNCTION {integration}.outbox_events_append_only();
