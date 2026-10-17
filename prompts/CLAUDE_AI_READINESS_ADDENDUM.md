# Add-on to the Claude Master Build Prompt — Future AI Engineering Readiness

**Copy this after the master build prompt, or attach the `10_AI_ENGINEERING_READINESS.md` and `11_AI_EVALUATION_AND_EXPERIMENTS.md` documents.**

I have an important additional long-term goal: after I build reliable ingestion, CDC, batch/streaming pipelines and a warehouse, I will also use the same synthetic e-commerce ecosystem for **AI engineering** projects.

Design the SOURCE DATA GENERATOR today so it can later support:

1. Product recommendation systems using exposures/impressions, searches, views, clicks, carts, purchases, repeat visits and temporal customer/product context.
2. Semantic search and text embeddings using realistic product titles, descriptions, attributes, categories and search interactions.
3. Retrieval-augmented generation (RAG) using fully synthetic versioned product guides, FAQs, return/shipping policies, support knowledge-base articles and resolved support tickets.
4. Demand forecasting using historical product-level orders, inventory availability, price/promotion changes and calendar effects.
5. Fraud/anomaly detection using synthetic payment/refund behavior, imbalanced labels, event sequences and delayed investigation outcomes.
6. Customer support NLP and later AI order-support agents with read-only order/shipment tools and auditable synthetic workflows.

### Non-negotiable data design

- Keep stable identifiers across PostgreSQL transactions, JSONL events, CSV feeds and text documents.
- Preserve UTC event time, source commit time, arrival time, effective start/end and version metadata where applicable.
- Record search/product **impressions/exposures** as well as interactions, so recommendation evaluation does not assume that every unseen product is a negative.
- Maintain catalog, pricing, inventory and knowledge-document history to enable **point-in-time correct** feature reconstruction and RAG answers.
- Keep original human-readable synthetic text and document sources; embeddings are derived artifacts, not replacements for source content.
- Include `document_id`, `document_version`, `section_id`, language and valid date/time windows for RAG citations.
- Generate controlled truth labels, relevance judgments, and benchmark questions **in a restricted private evaluation dataset** separate from the raw data visible to learner pipelines or AI inference.
- Enforce chronological splits, delayed labels, reproducible scenario seeds and independent held-out evaluation cases.
- Use only fully synthetic data; no real private data, copyrighted scraped documentation, or actual payment credentials.
- Make optional AI-focused datasets configurable and memory-bounded for 16 GB RAM and 2 TB storage.

### Scope control

Do NOT add model training, LLM serving, embeddings, a vector database, AI agents, feature-store infrastructure or a large knowledge corpus to Phase 1. Phase 1 remains a working PostgreSQL + file source generator with 10,000 consistent orders and tests.

**Now:** Reserve stable keys, timestamp contracts and schema extensibility; capture clickstream sessions and product interactions if included in the MVP.

**After core simulator correctness:** Add controlled product text, impressions/search events, reviews, support messages, versioned synthetic knowledge documents and segregated truth/evaluation fixtures as optional modules.

**Later, as a separate AI engineering project built by me:** Provide only source data contracts, test cases and an export/query guide. I will implement retrieval, recommendations, forecasting, fraud models, evaluation, model serving and agents independently.

### Output request

Update the generator roadmap and architecture to include an **AI-readiness milestone**. For each proposed AI data source, specify owning store (PostgreSQL vs files), schema, primary/join keys, timestamps/version fields, generation timing, scale settings, and private evaluation labels. Identify the minimum changes needed now and clearly defer all heavy AI components until later.
