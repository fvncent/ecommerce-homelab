# 11 — AI Engineering Experiments and Evaluation Plan (Future Project)

This document is a **future, separate learner-owned AI application specification**. It does not mean the e-commerce data generator implements recommendation training, RAG, fraud classifiers, embeddings or agents today.

## End-to-end project separation

1. **Synthetic source generator:** create coherent PostgreSQL transactions, raw event logs, product/policy/support text, file deliveries and private truth artifacts.
2. **Your data engineering project:** ingest/clean/version sources, build time-aware feature tables, retrieval corpora and test fixtures; own orchestration and quality SLAs.
3. **Your AI engineering project:** retrieval pipelines, offline training, model/LLM inference, agents, evaluations, monitoring and deployment.

Do not let AI code read private labels/expected answers at inference time; expose them only to evaluation after predictions are frozen.

## Example experiment ladder

| Sequence | Project | Minimal dataset | Baseline | Evaluation |
|---|---|---|---|---|
| A | Semantic product search | Catalog descriptions, search queries, relevance judgments | Exact/keyword search | Recall@K, MRR/nDCG, response latency |
| B | Customer support RAG | Versioned knowledge articles, questions, reference passages | BM25 or keyword retrieval + cited answers | Evidence recall, groundedness, correct abstention, answer accuracy |
| C | Product recommendations | Impression logs, customer sessions, clicks and purchases | Popularity and item co-occurrence | Recall@K / NDCG@K, coverage, temporal holdout |
| D | Demand forecasting | Order items, price, holidays, stockouts, returns | Seasonal naive / moving average | MAE, WAPE where defined, backtest by date |
| E | Fraud/anomaly detection | Payment retries, refunds, velocities, delayed truth | Heuristics/logistic regression | Precision/recall, PR-AUC, alert workload, delay |
| F | Order-support agent | Read-only order status tools and policy corpus | Deterministic workflow | Task success, correct tool usage, unsupported-action rate |

**Do not compare models using artificially easy random splits.** Establish baseline accuracy, dataset shift scenarios, class balance, cost and latency before claiming improvements.

## Private machine-readable evaluation records

Store ground truth in a restricted directory such as `/srv/ecommerce-data/private_ai_truth/` inaccessible to application/inference containers. These are **illustrative future schemas**, not generated files.

### RAG judgment example

```json
{"eval_id":"rag_0001","as_of":"2026-04-10T10:00:00Z","query":"Can I return a damaged item after delivery?","expected_doc_ids":["policy_returns_v2"],"expected_section_ids":["policy_returns_v2#damaged-items"],"expected_facts":["contact support within policy window","provide order identifier"],"should_abstain":false,"truth_version":1}
```

### Recommendation judgment example

```json
{"eval_id":"rec_0001","user_id":"cus_007","session_id":"ses_052","prediction_time":"2026-04-10T10:00:00Z","candidate_product_ids":["prd_001","prd_002","prd_003"],"relevant_product_ids":["prd_003"],"label_observed_at":"2026-04-11T10:00:00Z","truth_version":1}
```

### Delayed fraud label example

```json
{"eval_id":"fraud_0001","payment_id":"pay_012","prediction_time":"2026-04-10T10:00:00Z","fraud_label":true,"label_observed_at":"2026-04-24T09:00:00Z","scenario_id":"synthetic_refund_abuse_v1"}
```

The label may only be used for training/evaluation **after** `label_observed_at`, and features must be calculated from facts observable at `prediction_time`. Do not encode label-identifying scenario flags as model inputs.

## Evaluation controls

- Keep train/validation/test periods and scenario seeds in versioned configuration.
- Evaluate both typical and adverse cases: late events, stale documents, canceled orders, stockouts, cold-start products, input schema changes and unanswerable questions.
- Compare every AI approach to a cheap, interpretable baseline before adding GPU-heavy solutions.
- Maintain run manifests: source snapshot/version, feature code version, model/version, prompt version, indexing date, seed, prediction time window, metrics and errors.
- Preserve reproducible evaluation sets and prevent test-tuning leakage; don't overwrite held-out labels.
- RAG answers should cite exact synthetic `document_id` and section identifiers; verify cited text really supports the answer.
- Agent order-support tools must be read-only initially. Any later state-changing tools require a separately isolated sandbox, explicit authorization and auditable actions; never mutate source-of-truth orders just because a language model requested it.

## Suggested future repository boundary

```text
homelab/
  ecommerce-source-generator/    # Claude-built source simulator
  ecommerce-data-platform/       # Your batch/streaming pipelines
  ecommerce-ai-lab/              # Your later AI engineering work
    data_contracts/
    retrieval/
    recommendations/
    forecasting/
    fraud/
    agents/
    evaluation/
    configs/
    results/
```

The three projects may share identifiers and read-only datasets, but should not share private truth credentials or write paths. Promotion to later stages requires documented reliability, not a large row count alone.
