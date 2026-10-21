-- Warehouses and inventory (Phase 3D). Stock changes only through append-only inventory_movements, written in the
-- same transaction as the change they come from; an inventory row is the sum of its cell's movements (the loader
-- applies each chunk's deltas; `validate` checks the sum). A movement's product is its order line's product:
-- composite FK on (order_item_id, product_id).
ALTER TABLE {app}.order_items ADD CONSTRAINT order_items_item_product_key UNIQUE (order_item_id, product_id);

CREATE TABLE {app}.warehouses (
    warehouse_id text PRIMARY KEY CHECK (warehouse_id ~ '^wh_[1-9][0-9]*$'),
    name         text NOT NULL,
    region       text NOT NULL CHECK (region IN ('NA', 'EU', 'APAC')),
    capacity     integer NOT NULL CHECK (capacity > 0),  -- nominal units, not enforced against stock
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL CHECK (updated_at >= created_at)
);

CREATE TABLE {app}.inventory (
    warehouse_id text NOT NULL REFERENCES {app}.warehouses (warehouse_id),
    product_id   text NOT NULL REFERENCES {app}.products (product_id),
    on_hand      integer NOT NULL,
    reserved     integer NOT NULL,
    updated_at   timestamptz NOT NULL,  -- time of the latest movement
    PRIMARY KEY (warehouse_id, product_id),
    CHECK (reserved >= 0 AND reserved <= on_hand)  -- so on_hand >= 0 and available = on_hand - reserved >= 0
);
CREATE INDEX ON {app}.inventory (product_id);

CREATE TABLE {app}.inventory_movements (
    movement_id    text PRIMARY KEY,
    warehouse_id   text NOT NULL,
    product_id     text NOT NULL,
    movement_type  text NOT NULL,
    quantity       integer NOT NULL CHECK (quantity > 0),
    on_hand_delta  integer NOT NULL,
    reserved_delta integer NOT NULL,
    occurred_at    timestamptz NOT NULL,
    order_item_id  text,
    run_id         text NOT NULL REFERENCES {meta}.generation_runs (run_id),
    FOREIGN KEY (warehouse_id, product_id) REFERENCES {app}.inventory (warehouse_id, product_id),
    FOREIGN KEY (order_item_id, product_id) REFERENCES {app}.order_items (order_item_id, product_id),
    -- id, order line and deltas per type (generate.MOVE_DELTAS); IS TRUE: a NULL comparison must not pass
    CHECK ((CASE movement_type
        WHEN 'RECEIPT' THEN order_item_id IS NULL AND (on_hand_delta, reserved_delta) = (quantity, 0)
            AND movement_id = 'mov_rcv_' || warehouse_id || '_' || product_id
        WHEN 'REPLENISHMENT' THEN order_item_id IS NULL AND (on_hand_delta, reserved_delta) = (quantity, 0)
            AND movement_id ~ ('^mov_rpl_' || warehouse_id || '_' || product_id || '_[1-9][0-9]*$')
        WHEN 'RESERVE' THEN (on_hand_delta, reserved_delta) = (0, quantity)
            AND movement_id = 'mov_' || order_item_id || '_rsv'
        WHEN 'RELEASE' THEN (on_hand_delta, reserved_delta) = (0, -quantity)
            AND movement_id = 'mov_' || order_item_id || '_rel'
        WHEN 'SHIP' THEN (on_hand_delta, reserved_delta) = (-quantity, -quantity)
            AND movement_id = 'mov_' || order_item_id || '_shp'
        ELSE false END) IS TRUE)
);
-- serves reading in time order (incremental extraction)
CREATE INDEX inventory_movements_occurred ON {app}.inventory_movements (occurred_at, movement_id);

-- Append-only: a movement, once committed, is never changed or removed.
CREATE FUNCTION {app}.inventory_movements_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'inventory_movements is append-only (% refused)', TG_OP;
END $$;
CREATE TRIGGER inventory_movements_no_update_delete BEFORE UPDATE OR DELETE ON {app}.inventory_movements
    FOR EACH ROW EXECUTE FUNCTION {app}.inventory_movements_append_only();
CREATE TRIGGER inventory_movements_no_truncate BEFORE TRUNCATE ON {app}.inventory_movements
    FOR EACH STATEMENT EXECUTE FUNCTION {app}.inventory_movements_append_only();
