# Migration: VictoriaMetrics → QuestDB

Status: **planned, not started.** Nothing in `scripts/` targets QuestDB yet.

## Why

VictoriaMetrics cannot express "the newest delivered value wins".

The provider sends overlapping 5-day files daily, so each 15-min slot arrives
5-7 times, and ~2.6% of overlapping slots are **revised** — sometimes downward
(meter `0050170B`, 2026-05-22T00:00: `0.003` on delivery 20260527 → `0.002` on
20260605). VM offers only two behaviours, neither of which is "newest":

- **without** `-dedup.minScrapeInterval`: every copy is retained and `sum()`
  counts the slot 5-7 times (measured: June 2026 consumption `sum(E66)/E31` =
  2.0-6.5)
- **with** it: ingestion keeps the **maximum** value per
  `(metric, labels, timestamp)`, so a downward revision never lands — totals
  read **+1.1467%** high (+2144.968 kWh on 187063.4 kWh; worst slot +9.383 kWh)

`scripts/vm_upsert.py` (287 lines + a SQLite side-store) works around this by
detecting revisions locally, then **deleting the whole series and replaying its
full history** — VM has no per-timestamp delete. That delete→reimport window is
non-atomic: if the reimport fails, the series is missing until someone reruns the
file.

QuestDB's `DEDUP UPSERT KEYS` is genuine last-write-wins ("If the row differs,
replaces the old row"), so all of that collapses into a plain `INSERT`.

## What we lose, and the mitigation

**`vm_upsert` refuses stale writes; QuestDB will not.** The current
implementation compares the delivery date of the incoming sample against the
stored one and rejects older ones (the reverse-order test refuses 554,880
samples). QuestDB's LWW does not consult any column — whatever writes last wins.
Replaying delivery `20260527` after `20260605` silently regresses 4 days.

QuestDB has no conditional upsert (no `WHERE EXCLUDED.delivery >= ...`), so the
options are read-before-write — reintroducing the complexity we are removing — or
**enforcing replay order**. We enforce order:

- Production: safe already. The watcher batches by delivery date and flushes on
  date change (`watch_ftproot.py:201-210`), so batches are chronological and
  files *within* one batch all share a delivery date, making intra-batch order
  irrelevant.
- Startup rescan: safe already — `sorted(watch_dir.glob("*.xml"))` at
  `watch_ftproot.py:568` puts the `YYYYMMDD` prefix in chronological order.
- **Manual replay: must be sorted.** `reprocess_all_data.sh` currently does
  `mv .../archive/*.xml .../incoming/`, relying on the watcher's sort. That
  happens to be correct, but it is now a *correctness* requirement rather than a
  batching nicety, so it needs a comment saying so — and any new replay tooling
  must sort explicitly.

This trade is deliberate: one documented ordering rule in place of ~300 lines of
stateful reconciliation plus a non-atomic rewrite window.

## Schema

```sql
CREATE TABLE cel_energy (                 -- E66, per-meter
  ts           TIMESTAMP,
  meter_id     SYMBOL,
  direction    SYMBOL,          -- consumption | production
  segment      SYMBOL,          -- cel | grid | total
  product_code SYMBOL,
  community_id SYMBOL,
  value        DECIMAL(12, 3),  -- exact; matches the source's 3 decimals
  code_type    SYMBOL,          -- payload: derivable from product_code
  condition    SYMBOL           -- payload: provider revises it (never a key)
) TIMESTAMP(ts) PARTITION BY MONTH WAL TTL 2 YEARS
DEDUP UPSERT KEYS(ts, meter_id, direction, segment, product_code, community_id);

CREATE TABLE cel_community_energy (       -- E31, community aggregate
  ts             TIMESTAMP,
  direction      SYMBOL,
  segment        SYMBOL,
  product_code   SYMBOL,
  community_id   SYMBOL,
  value          DECIMAL(12, 3),
  code_type      SYMBOL,
  community_type SYMBOL,
  grid_area      SYMBOL,
  condition      SYMBOL
) TIMESTAMP(ts) PARTITION BY MONTH WAL TTL 2 YEARS
DEDUP UPSERT KEYS(ts, direction, segment, product_code, community_id);

CREATE TABLE cel_ingest_log (             -- provenance, ~1 row per file
  ts           TIMESTAMP,
  delivery     SYMBOL,
  file_name    SYMBOL,
  rows_written LONG,
  outcome      SYMBOL           -- ingested | skipped | failed
) TIMESTAMP(ts) PARTITION BY MONTH WAL;
```

Two tables for E66/E31 rather than one metric-name column: a `sum()` can no
longer accidentally mix per-meter readings with the community aggregate that
already contains them. Structural, not a naming convention.

### Key design rules

**`condition` must never be an upsert key.** The provider revises a slot's grade
across deliveries (estimated → measured). Keyed, one slot becomes two rows and
every `sum()` double-counts it. As payload it is finally *storable* — VM could
not keep it at all (see the notes in `transform_to_datapoints`), so Q16c-style
questions ("do these 0.00 kWh readings carry Condition 21?") become plain SQL.

**`code_type` is payload, not a key.** It is functionally dependent on
`product_code` (`2404050010123`/`...124` → `VSENationalCode`, `8716867000030` →
`ebIXCode`). Keyed, a provider-side encoding change would create a phantom
parallel series that silently double-counts instead of overwriting.

**No `project` column.** It was the constant `'cel'` on every row — a workaround
for VM's flat namespace. The table name scopes it now. Dashboard filters
`project="cel"` simply disappear.

**No `delivery` column on the data tables.** It was load-bearing in `vm_upsert`
(the staleness comparison), but LWW cannot use it, and it cannot even *detect* a
regression after the fact: `DEDUP UPSERT KEYS` guarantees exactly one row per
key, so there is no second row to compare against. Provenance lives in
`cel_ingest_log` (~1 row per file) and in the archived XML filenames, instead of
being duplicated across ~25M rows. It would also defeat QuestDB's
skip-identical-row optimisation on every overlapping row, since it changes daily.

### Why `DECIMAL(12,3)`

`DECIMAL(precision, scale)`, precision 1-76, exact fixed-point. Storage tiers:
10-18 digits → DECIMAL64 (8 bytes); ≥19 → DECIMAL128. The docs advise keeping
precision ≤18 because DECIMAL64 is faster.

`(12,3)` and `(15,3)` are both DECIMAL64 — identical storage and speed. 12 allows
999,999,999.999, far beyond any community total, so the extra digits buy nothing.

Two consequences:

- **Scale 3 must match the source exactly.** Source data is 3 decimals, so this
  is lossless — but a 4-decimal value would be **silently rounded at ingest**.
  Phase 2 adds a parser assertion.
- **Multiplication sums precision:** `DECIMAL(12,3) * DECIMAL(12,3)` →
  `DECIMAL(24,6)`, which promotes to DECIMAL128 and loses the fast path. In
  dashboards multiply by the integer literal `4`, never `4.000m`.

Decimal literals need an `m` suffix (`0.001m`); QuestDB does **not** implicitly
convert double → decimal, so `WHERE value > 0.5` and `... > 0.5m` are different
comparisons. Aggregate expressions mixing the two will surprise you.

## Ingestion

`Observation.value` becomes `decimal.Decimal` parsed from the XML text, not
`float` — parsing to float first defeats the point of an exact column.

Two paths, in order of preference:

1. **PG-wire (`psycopg` 3.3.4) with `executemany`.** `decimal.Decimal` maps to
   DECIMAL natively with no suffix handling, and DEDUP applies to ordinary
   inserts on WAL tables. ~48k rows/delivery is comfortable. Recommended for
   Phase 2 — correctness first.
2. **ILP over HTTP (`questdb` 5.0.0).** Faster. Decimals require a `d` suffix
   (`price=30000.50d`), and the documented alternative is sending the value as a
   **string** into a pre-existing decimal column, which QuestDB casts. Whether
   the Python client's `Sender.row()` accepts `decimal.Decimal` directly needs
   checking — the confirmed decimal support is on the pandas/Arrow path. Note
   `questdb` 5.0.0 requires Python ≥3.10; the image is 3.11.

Either way the tables must be **created up front**: ILP auto-creation would
produce a table with no DEDUP and a default `DECIMAL(18,3)`.

`scripts/questdb_writer.py` replaces `vm_upsert.py`. No local state, no
delete-and-rewrite, no `SampleStore` — just an insert. Expected shape:

```python
def write(data_points, dsn, table) -> Dict[str, int]:
    """Insert rows; QuestDB DEDUP UPSERT KEYS makes the last write win."""
```

## Phases

| # | Work | Deliverable |
|---|---|---|
| 0 | Add `questdb` service to compose alongside VM | `docker-compose.yml` |
| 1 | Schema + idempotent init | `scripts/questdb_schema.sql` |
| 2 | Writer; `Decimal`-valued observations; watcher writes to QuestDB **and** VM behind a config flag | `scripts/questdb_writer.py` |
| 3 | Port the test suite to LWW semantics | `tests/test_questdb_writer.py` |
| 4 | Replay all archived XML **sorted by delivery prefix** | — |
| 5 | Convert 27 dashboard expressions to SQL | `grafana-dashboards/*.json` |
| 6 | Validate QuestDB vs VM day-by-day | — |
| 7 | Remove VM, `vm_upsert.py`, `send_to_victoriametrics.py`, VM datasource | — |

No data migration: history is rebuilt by replaying the XML archive (Phase 4).

### Phase 3 — what the tests must pin

`tests/conftest.py` currently has `FakeVictoriaMetrics`, which models
**max-on-duplicate** and whole-series delete. The QuestDB fake models **LWW** on
the upsert key. Port from `tests/test_vm_upsert.py` (21 tests):

- new rows insert; re-sending an identical row is a no-op
- upward **and** downward revision both end at the newest value
- **ascending** replay of the real 8-delivery corpus (20260527-20260603, 869
  files, 382,560 observations → 115,008 unique slots, 100 series) matches a
  "newest wins" oracle
- **descending** replay asserts the *documented regression* — this is now
  expected behaviour, so pin it rather than let it surprise someone later
- the corpus still contains downward revisions (1,728 down / 13,155 up),
  guarding the premise of the golden test
- `condition` revision updates in place and does **not** create a second row
  (the double-count regression)
- a 4-decimal value is rejected, not silently rounded
- `Decimal` round-trips exactly: `0.002` stays `0.002`

Drop as obsolete: `stale`/out-of-order refusal, `selector_for` escaping, the
delete-then-rewrite paths, `fail_next_delete`, store-survives-reopen.

### Phase 5 — dashboard conversion

27 expressions (9 in `cel_energy_overview.json`, 18 in
`grafana-dashboard-e31-v2.json`). Datasource: the official plugin is
`questdb-questdb-datasource` — **v0.1.8, pre-1.0, signed `commercial`**. Fallback
is Grafana's built-in Postgres datasource on port 8812, which the QuestDB docs
say works but configures differently. **Convert one panel before the other 26**
so the plugin decision is settled while VM is still live and comparable.

The `avg_over_time(...[$__interval:15m]) * 4` idiom exists only to turn 15-min
kWh into kW under PromQL's model. Note `sum(value) * 4` is **wrong** for a bucket
wider than 15 min (a 1h bucket of 4×1 kWh gives 16, not 4 kW) — average per slot
first, then scale:

```sql
-- kW, summed across meters then averaged over the bucket
SELECT ts AS time, avg(total) * 4 AS kw FROM (
  SELECT ts, sum(value) AS total
  FROM cel_energy
  WHERE $__timeFilter(ts) AND segment = 'cel' AND direction = 'consumption'
  SAMPLE BY 15m
) SAMPLE BY $__sampleByInterval FILL(NULL);
```

Often the panel reads better as energy per bucket, dropping the `* 4` entirely:
`SELECT ts AS time, sum(value) AS kwh ... SAMPLE BY $__sampleByInterval`.

Self-consumption ratios lose `increase()`, which was always an odd fit for a
gauge:

```sql
SELECT 100 * sum(CASE WHEN segment = 'cel'  THEN value ELSE 0m END)
           / sum(CASE WHEN segment IN ('cel','grid') THEN value ELSE 0m END) AS pct
FROM cel_energy
WHERE $__timeFilter(ts) AND direction = 'consumption';
```

Plugin macros are `$__timeFilter(col)`, `$__sampleByInterval`, `$__fromTime` /
`$__toTime`, and `$__conditionalAll(cond, $var)` — the last one replaces the
`meter_id=~".*${meter_id}"` regex idiom and handles the "All" case properly.
There is **no** `$__interval` macro on this datasource (that is Grafana/Prometheus
naming); the Postgres fallback has a different set again, so macros must be
revisited if you switch datasources.

Bonus: `toolbox/diagnose_validation_gap.py` (a ~500-line Python E66-vs-E31
reconciliation) becomes a SQL join and can move into a dashboard panel.

### Phase 6 — validation

QuestDB should read **~1.15% lower** than VM on the same range. That gap is the
max-dedup inflation being removed: it is the **success signal, not a bug**.
Confirm per day over 20260430-20260727, and check the known anomalies survive
(meter `0046782G` producing 0.0 from 2026-06-23; `0858140M` zero on 82/84 days).
Rewrite `scripts/validate_daily_balance_vm.py` as the QuestDB equivalent.

## Security

QuestDB OSS ships the web console and REST API on **9000 with no meaningful
auth** — the same exposure class as VM's unauthenticated `/api/v1/write`. Keep
9000 and 8812 on `cel-network` only; publish nothing but Grafana through
cloudflared. Do not expose the console to "just have a look".

## Rollback

VM keeps running through Phase 6 with dual writes (Phase 2), so rollback is
flipping the config flag off. `vm_upsert.py` and its 21 tests stay in the tree
until Phase 7 — deployed and working, not deleted on plan.

## Open items

- `questdb` Python client: does `Sender.row()` accept `decimal.Decimal`, or is
  the string-cast route required? (Phase 2 — moot if we take the PG-wire path)
- Grafana plugin v0.1.8 viability vs the Postgres datasource fallback (Phase 5)
- Does any source file ever carry more than 3 decimals? (Phase 2 assertion)

Resolved while writing this guide: `DEDUP UPSERT KEYS` is last-write-wins and
requires WAL + the designated timestamp in the keys; `DECIMAL` exists in OSS with
precision 1-76; TTL is OSS-only (**Enterprise rejects non-zero `SET TTL`** and
requires a storage policy instead — relevant only if this ever moves to
Enterprise); `TTL 2 YEARS` is valid (units `HOUR/DAY/WEEK/MONTH/YEAR`, so a bare
`2y` is also accepted); the Grafana macro is `$__sampleByInterval`.
