-- QuestDB schema for the CEL energy pipeline. See QUESTDB.md.
--
-- Run with:  python3 scripts/questdb_init.py
-- Idempotent: every statement is IF NOT EXISTS, so re-running is a no-op.
--
-- The tables MUST exist before any ingestion. QuestDB auto-creates a table on
-- first ILP/insert if it is missing -- but without DEDUP and with a default
-- DECIMAL(18,3), which silently reintroduces the duplicate-counting bug this
-- whole migration exists to fix.

-- E66: per-meter readings.
CREATE TABLE IF NOT EXISTS cel_energy (
  ts           TIMESTAMP,
  meter_id     SYMBOL,
  direction    SYMBOL,          -- consumption | production
  segment      SYMBOL,          -- cel | grid | total  (total = cel + grid)
  product_code SYMBOL,
  community_id SYMBOL,
  value        DECIMAL(12, 3),  -- exact fixed point; source is always 3 dp
  code_type    SYMBOL,          -- payload: derivable from product_code
  -- Payload, NEVER a dedup key. The provider revises a slot's condition across
  -- overlapping deliveries (estimated one day, measured the next). As a key,
  -- that slot would become TWO rows and every sum() would double-count it.
  condition    SYMBOL
) TIMESTAMP(ts) PARTITION BY MONTH WAL
DEDUP UPSERT KEYS(ts, meter_id, direction, segment, product_code, community_id);

-- E31: community aggregates. Separate table (not a metric-name column) so a
-- sum() cannot mix per-meter readings with the aggregate that already contains
-- them -- the double-count is prevented structurally, not by naming convention.
CREATE TABLE IF NOT EXISTS cel_community_energy (
  ts             TIMESTAMP,
  direction      SYMBOL,
  segment        SYMBOL,
  product_code   SYMBOL,
  community_id   SYMBOL,
  value          DECIMAL(12, 3),
  code_type      SYMBOL,
  community_type SYMBOL,
  grid_area      SYMBOL,
  condition      SYMBOL           -- payload, never a key -- as in cel_energy
) TIMESTAMP(ts) PARTITION BY MONTH WAL
DEDUP UPSERT KEYS(ts, direction, segment, product_code, community_id);

-- Provenance: ~1 row per file, rather than a `delivery` column repeated on every
-- sample. Answers "did delivery 20260722 land?" and "which
-- files failed?" without duplicating the delivery date across ~25M rows.
-- No DEDUP: reprocessing a file is a genuinely new ingestion event.
CREATE TABLE IF NOT EXISTS cel_ingest_log (
  ts           TIMESTAMP,
  delivery     SYMBOL,          -- YYYYMMDD filename prefix
  file_name    SYMBOL,
  document_type SYMBOL,         -- E66 | E31 | unknown
  rows_written LONG,
  outcome      SYMBOL           -- ingested | skipped | failed
) TIMESTAMP(ts) PARTITION BY MONTH WAL;
