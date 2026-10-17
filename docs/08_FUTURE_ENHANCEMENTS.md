# 08 — Future Enhancement Backlog

## Priority A — After MVP correctness (no upgrade required)

| Feature | Value | Prerequisites |
|---|---|---|
| Live simulation clock with time multiplier | Continuous business changes, replay and backfills | Stable state machine |
| Scenario Manager with presets | Reproducible Black Friday, supplier failure, payment outage | Config system + metrics |
| File delivery schedule | Real late/duplicate/missing partner feeds | File manifests/checkpoints |
| Data quality scoring | Compare downstream detections to private ground truth | Anomaly ledger |
| Deterministic historical replay | Rebuild from chosen date without losing referential integrity | Versioned seed/config |
| Change log / transactional outbox | Event publication and eventual CDC training | DB transactions |
| Resource-aware job limits | Prevent server overload | Monitoring and queue |
| Simple Streamlit UI | Launch/monitor runs and inspect manifests | CLI and engine complete |

## Priority B — After 32 GB RAM or proven low-concurrency operation

| Feature | Value | Tradeoff |
|---|---|---|
| Debezium + Kafka-compatible broker | Capture database INSERT/UPDATE/DELETE | WAL monitoring and extra RAM |
| Airflow orchestration | Scheduling, backfills, retries, SLA checks | Scheduler services |
| MinIO S3-compatible source or landing zone | Cloud-like object storage locally | Extra services/operational surface |
| Apache Spark single-node | Large data joins, partition tuning and skew | Java and heap demand |
| dbt + DuckDB or PostgreSQL warehouse | Reliable SQL modeling and testing | Isolate from OLTP DB load |
| OpenTelemetry / Prometheus + Grafana | Metrics, traces and reliability reporting | Retention management |

## Priority C — Later advanced reliability / platform design

- Iceberg/Delta Lake and incremental merge tables.
- Contract-first event schema registry and Avro evolution.
- Warehouse SCD Type 2 and replayable correction events.
- CDC snapshot and incremental consistency checks.
- Chaos exercises: disk-threshold, DB failover, broker outage, slow consumers.
- Infrastructure-as-code and GitHub Actions CI.
- Multi-region timestamps, currencies, locale-specific holidays and taxes.
- Vendor service level agreements, realistic cutoff windows and late settlement differences.
- Data lineage, source quality scorecards and freshness SLO dashboards.
- Synthetic workload benchmarking against two different pipeline designs.

## Hardware plan — conditional

1. **Check storage medium and RAM slots first** using the hardware guide. If PostgreSQL is on HDD, consider a dedicated SSD/NVMe before adding more data-heavy services.
2. **RAM target: 32 GB (2×16 GB matched DDR4 UDIMM kit)** if motherboard supports it; inspect existing configuration before buying.
3. **64 GB RAM** may make bigger Spark + Kafka experiments more convenient if the motherboard supports it, but it is not essential for the generator MVP.
4. Keep backups **off the sole 2 TB physical drive**; external drive or remote backup target.
5. Prefer monitoring-based upgrades over buying hardware based on arbitrary row counts.

## Prioritization rule

Promote an enhancement only when the previous stage passes checks for data correctness, restartability, resource usage and actual learning value. Do not install extra services simply because they are fashionable.

## Priority D — Future AI engineering (after trusted data pipelines)

| Feature | Foundation dataset | Resource strategy |
|---|---|---|
| Product semantic search + embeddings | Versioned catalog descriptions, search queries, relevance truth | Begin with small samples and CPU-friendly embeddings |
| RAG customer support chatbot | Synthetic versioned FAQs, return/shipping policies, product guides and resolved tickets | Start with retrieval benchmark, then optional LLM API or small local model |
| Product recommendation systems | Exposure/impression and click/cart/purchase sessions, price/inventory as-of event time | Start with popularity/item similarity baselines |
| Demand forecasting | Historical item sales, promotions, price, stockouts and calendar | Start with SQL/DuckDB + classical forecasting baselines |
| Fraud detection | Payments, retries, refunds and delayed synthetic fraud labels | Focus on class imbalance, temporal splits and PR-AUC |
| AI order-support agent | Read-only order/shipping tools plus synthetic policy knowledge | Start read-only; later isolated sandbox writes and audit trails |
| Model evaluation and monitoring | Private gold Q&A, product relevance judgments, label history and scenario seeds | Store fixtures separately; avoid test leakage and ground-truth exposure |

**Separate the three codebases:** source generator, your own data engineering pipeline, your own AI engineering application. Preserve original text, event time, document IDs/versions and provenance from the start. Avoid deploying vector databases or LLM servers on the 16 GB host before proving that they fit the workload; 32 GB helps, but a GPU may be needed for demanding local models. Details: [10_AI_ENGINEERING_READINESS.md](10_AI_ENGINEERING_READINESS.md), [11_AI_EVALUATION_AND_EXPERIMENTS.md](11_AI_EVALUATION_AND_EXPERIMENTS.md).
