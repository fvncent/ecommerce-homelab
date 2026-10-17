# 03 — Data Model and Data Contracts

## Relational source model

Minimum tables; Claude should add indexes, check constraints, status enums / lookup tables, timestamps and migration scripts.

| Table | Primary key | Important fields / relationships |
|---|---|---|
| customers | customer_id | created_at, country, segment, status, updated_at |
| sellers | seller_id | region, display_name, status |
| categories | category_id | parent_category_id (nullable), name |
| products | product_id | seller_id FK, category_id FK, price NUMERIC, currency, active |
| warehouses | warehouse_id | region, capacity |
| inventory | (warehouse_id, product_id) | on_hand, reserved, updated_at |
| orders | order_id | customer_id FK, placed_at, status, currency, subtotal, discount, tax, shipping, total |
| order_items | order_item_id | order_id FK, product_id FK, quantity, unit_price, discount, tax, line_total |
| payment_attempts | payment_id | order_id FK, attempt_no, provider, method, state, amount, currency |
| shipments | shipment_id | order_id FK, warehouse_id FK, created_at, dispatched_at, delivered_at, state |
| shipment_items | (shipment_id, order_item_id) | shipped_quantity |
| returns | return_id | order_item_id FK, quantity, requested_at, reason, state |
| refunds | refund_id | return_id or payment_id FK, amount, currency, issued_at, status |
| cart_events | event_id | customer/session, product, action, event_time |
| outbox_events | event_id | aggregate_type, aggregate_id, event_type, payload JSONB, created_at, schema_version |
| generation_runs | run_id | seed, config_hash, state, timestamps, counts, version |

**Scope notes:** For MVP, focus on the subset needed for 10,000 coherent orders; defer rich support/reviews/promotions until relationships and replay are proven. Any materialized `orders.total` is the sum of explicit price components and must reconcile with order items; payment settlements and refunds have their own timing rules.

## Lifecycle contracts

- Order: `CREATED -> CONFIRMED -> PROCESSING -> SHIPPED -> DELIVERED`; optional cancellation from eligible earlier states. Split shipments may mean order-level `PARTIALLY_SHIPPED`.
- Payment attempt: `INITIATED -> AUTHORIZED -> CAPTURED -> SETTLED`, or `FAILED` / `VOIDED` as valid branches; refunds separate.
- Shipment: `CREATED -> PICKED -> IN_TRANSIT -> DELIVERED` (plus failure/return branches).
- Return: `REQUESTED -> APPROVED -> RECEIVED -> INSPECTED -> REFUNDED`; may be rejected.
- Inventory: moves in explicit purchase/reservation/release/replenishment transactions; non-negative quantities unless a configurable exceptional business rule permits otherwise.

Test legal transitions; don't assume every order has exactly one payment or one shipment.

## Field conventions

- IDs: stable deterministic unique strings/UUIDs or integer IDs from a documented strategy. All sources use the same business identifiers.
- Currency: ISO currency code; store monetary amount as integer minor units or precise database NUMERIC/Decimal, never binary float for reconciliation.
- Time: source timestamps stored UTC with timezone information; separate local business calendar if needed.
- `event_time`: when business action occurred. `committed_at`: source database transaction time. `arrived_at`: delivery/ingest time. These may differ legitimately.
- Schema: `schema_version` on events; format change log and compatibility tests.
- PII: fake-only names/emails/addresses; use obviously synthetic domains and no payment card numbers.

## Example JSONL clickstream event

```json
{"event_id":"evt_000042","schema_version":1,"event_type":"product_viewed","customer_id":"cus_0014","session_id":"ses_099","product_id":"prd_0203","event_time":"2026-10-10T09:20:11Z","arrived_at":"2026-10-10T09:20:14Z","source_system":"web_app","payload":{"page":"product_detail"}}
```

## Example CSV supplier feed

```csv
supplier_id,product_id,warehouse_id,on_hand_as_of,available_qty,feed_date
sel_001,prd_0203,wh_01,2026-10-10T00:00:00Z,141,2026-10-10
```

## Controlled data quality anomalies

| Type | Example | Expected handling |
|---|---|---|
| Duplicate delivery | Same event ID in two files | Deduplicate by event ID / business key |
| Late arrival | Payment confirmation delivered 3 days late | Event-time semantics and watermarks |
| Schema evolution | Supplier adds `backorder_qty` | Schema versioning, compatibility |
| Bad record | Invalid CSV quoting or missing mandatory ID | Quarantine with diagnostics |
| Out-of-order update | Delivered notification before dispatched file arrives | State reconciliation / valid processing |
| Skew | Best-selling product 20% of traffic | Partition/skew optimization |
| Corrections | Reissued settlement row correcting old amount | Upsert by compound source key/version |
| Orphan raw row | File references missing order intentionally | Alert / quarantine; no silent join loss |

Clean canonical data must stay logically consistent. Deliberately bad rows belong in **external raw feeds** (or separately labeled test scenarios), and anomalies must have an exact private manifest with expected detections.

## Example manifest fields

`run_id`, `generator_version`, `config_sha256`, `seed`, `source_system`, `file_path`, `format`, `schema_version`, `start_event_time`, `end_event_time`, `records_written`, `bytes`, `sha256`, `anomaly_count`, `published_at`.

Avoid putting privileged anomaly ground truth in learner pipeline mounts; only reveal labels during testing/scoring.

## AI-compatible extension contracts (future, not MVP)

To support later AI engineering, add **optional** datasets after core e-commerce integrity is proven: versioned product descriptions/attributes; `search_sessions` and `product_impressions` (including exposure rank and price at exposure); typed click/cart/purchase events with `session_id`, `event_time` and `arrived_at`; synthetic reviews; synthetic support tickets/messages; Markdown FAQ/policy/product-guide documents with stable `document_id`, `section_id`, version and effective-dated applicability. Keep immutable source text alongside structured source IDs.

Never use observed post-prediction actions or private outcome labels as feature inputs. Labels and RAG relevance judgments must be in a non-mounted `private_ai_truth` directory with `label_observed_at`, scenario and seed. Training and evaluation should use point-in-time snapshots and held-out time ranges. See [10_AI_ENGINEERING_READINESS.md](10_AI_ENGINEERING_READINESS.md) and [11_AI_EVALUATION_AND_EXPERIMENTS.md](11_AI_EVALUATION_AND_EXPERIMENTS.md).
