-- Phase 1 MVP: the entities needed for coherent orders + the generator run/chunk ledger.
-- {app}/{integration}/{meta} are replaced with quoted schema identifiers from config.
-- IDs are deterministic prefixed strings chosen by the generator (no sequences), so a re-run of a
-- chunk produces the same keys and conflicts instead of duplicating.

CREATE SCHEMA IF NOT EXISTS {app};
CREATE SCHEMA IF NOT EXISTS {integration};

-- ---------- reference / master data ----------

CREATE TABLE {app}.sellers (
    seller_id    text PRIMARY KEY CHECK (seller_id ~ '^sel_[1-9][0-9]*$'),
    display_name text NOT NULL,
    region       text NOT NULL,
    status       text NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')),
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL CHECK (updated_at >= created_at)
);

CREATE TABLE {app}.categories (
    category_id        text PRIMARY KEY CHECK (category_id ~ '^cat_[1-9][0-9]*$'),
    parent_category_id text REFERENCES {app}.categories (category_id),
    name               text NOT NULL,
    CHECK (parent_category_id IS DISTINCT FROM category_id)
);
CREATE INDEX ON {app}.categories (parent_category_id);

CREATE TABLE {app}.customers (
    customer_id text PRIMARY KEY CHECK (customer_id ~ '^cus_[1-9][0-9]*$'),
    full_name   text NOT NULL,
    -- synthetic PII only: reserved example domains (RFC 2606)
    email       text NOT NULL UNIQUE CHECK (email ~ '^[^@\s]+@example\.(com|net|org)$'),
    country     char(2) NOT NULL CHECK (country ~ '^[A-Z]{2}$'),
    segment     text NOT NULL,
    status      text NOT NULL CHECK (status IN ('ACTIVE', 'INACTIVE', 'CLOSED')),
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL CHECK (updated_at >= created_at)
);

CREATE TABLE {app}.products (
    product_id  text PRIMARY KEY CHECK (product_id ~ '^prd_[1-9][0-9]*$'),
    seller_id   text NOT NULL REFERENCES {app}.sellers (seller_id),
    category_id text NOT NULL REFERENCES {app}.categories (category_id),
    name        text NOT NULL,
    price       numeric(12, 2) NOT NULL CHECK (price > 0),
    currency    char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    active      boolean NOT NULL,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL CHECK (updated_at >= created_at)
);
CREATE INDEX ON {app}.products (seller_id);
CREATE INDEX ON {app}.products (category_id);

-- ---------- transactions ----------
-- Money contract (cross-row parts verified by `validate`, per-row parts enforced here):
--   order_items.line_total = quantity * unit_price - discount + tax
--   orders.subtotal = SUM(quantity * unit_price), orders.discount = SUM(item discount),
--   orders.tax = SUM(item tax), orders.shipping is order-level,
--   orders.total = subtotal - discount + tax + shipping
--   items are priced in the order currency; payments must match it (FK below);
--   product currency vs order currency is a `validate` rule

CREATE TABLE {app}.orders (
    order_id    text PRIMARY KEY CHECK (order_id ~ '^ord_[1-9][0-9]*$'),
    customer_id text NOT NULL REFERENCES {app}.customers (customer_id),
    placed_at   timestamptz NOT NULL,
    status      text NOT NULL CHECK (status IN (
                    'CREATED', 'CONFIRMED', 'PROCESSING', 'PARTIALLY_SHIPPED',
                    'SHIPPED', 'DELIVERED', 'CANCELLED')),
    currency    char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    subtotal    numeric(14, 2) NOT NULL CHECK (subtotal >= 0),
    discount    numeric(14, 2) NOT NULL CHECK (discount >= 0 AND discount <= subtotal),
    tax         numeric(14, 2) NOT NULL CHECK (tax >= 0),
    shipping    numeric(14, 2) NOT NULL CHECK (shipping >= 0),
    total       numeric(14, 2) NOT NULL,
    updated_at  timestamptz NOT NULL CHECK (updated_at >= placed_at),
    CHECK (total = subtotal - discount + tax + shipping),
    UNIQUE (order_id, currency)  -- target for payment currency FK
);
CREATE INDEX ON {app}.orders (customer_id);
CREATE INDEX ON {app}.orders (placed_at);

CREATE TABLE {app}.order_items (
    order_item_id text PRIMARY KEY CHECK (order_item_id ~ '^itm_[1-9][0-9]*$'),
    order_id      text NOT NULL REFERENCES {app}.orders (order_id),
    line_no       integer NOT NULL CHECK (line_no > 0),
    product_id    text NOT NULL REFERENCES {app}.products (product_id),
    quantity      integer NOT NULL CHECK (quantity > 0),
    unit_price    numeric(12, 2) NOT NULL CHECK (unit_price >= 0),
    discount      numeric(14, 2) NOT NULL CHECK (discount >= 0 AND discount <= quantity * unit_price),
    tax           numeric(14, 2) NOT NULL CHECK (tax >= 0),
    line_total    numeric(14, 2) NOT NULL,
    UNIQUE (order_id, line_no),
    CHECK (line_total = quantity * unit_price - discount + tax)
);
CREATE INDEX ON {app}.order_items (product_id);

CREATE TABLE {app}.payment_attempts (
    payment_id text PRIMARY KEY CHECK (payment_id ~ '^pay_[1-9][0-9]*$'),
    order_id   text NOT NULL REFERENCES {app}.orders (order_id),
    attempt_no integer NOT NULL CHECK (attempt_no > 0),
    provider   text NOT NULL,
    -- no card numbers are ever stored
    method     text NOT NULL CHECK (method IN ('CARD', 'PAYPAL', 'BANK_TRANSFER', 'WALLET')),
    state      text NOT NULL CHECK (state IN (
                   'INITIATED', 'AUTHORIZED', 'CAPTURED', 'SETTLED', 'FAILED', 'VOIDED')),
    amount     numeric(14, 2) NOT NULL CHECK (amount > 0),
    currency   char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
    UNIQUE (order_id, attempt_no),
    FOREIGN KEY (order_id, currency) REFERENCES {app}.orders (order_id, currency)
);

-- ---------- generator ledger (not source data; downstream pipelines should ignore {meta}) ----------

CREATE TABLE {meta}.generation_runs (
    run_id            text PRIMARY KEY,
    generator_version text NOT NULL,
    seed              bigint NOT NULL,
    config_sha256     char(64) NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    profile           text NOT NULL,
    state             text NOT NULL CHECK (state IN ('RUNNING', 'COMPLETED', 'FAILED')),
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz CHECK (finished_at >= started_at),
    counts            jsonb NOT NULL DEFAULT '{}',
    error             text,
    CHECK ((state = 'RUNNING') = (finished_at IS NULL))
);
-- At most one RUNNING run: two concurrent runs would write the same deterministic IDs.
CREATE UNIQUE INDEX generation_runs_one_running ON {meta}.generation_runs ((true)) WHERE state = 'RUNNING';

-- One row per committed chunk, written in the same transaction as the chunk's data:
-- present => the chunk's rows are durable; absent => nothing from it was committed.
CREATE TABLE {meta}.generation_chunks (
    run_id       text NOT NULL REFERENCES {meta}.generation_runs (run_id),
    entity       text NOT NULL,
    chunk_no     integer NOT NULL CHECK (chunk_no >= 0),
    row_count    integer NOT NULL CHECK (row_count >= 0),
    completed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, entity, chunk_no)
);
