-- Rows written per table by each chunk (an orders chunk also writes order_items and payment_attempts), so run
-- totals and completeness checks come from the ledger instead of count(*) scans of the source tables.
ALTER TABLE {meta}.generation_chunks ADD COLUMN table_rows jsonb NOT NULL DEFAULT '{}';
