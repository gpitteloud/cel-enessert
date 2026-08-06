# QuestDB — the system of record

Everything the pipeline stores lands in QuestDB. This file is the reference for
*why* the schema looks the way it does, which parts of it are load-bearing, and
which traps in the Grafana datasource plugin cost real debugging time.

## The problem it solves

The provider sends overlapping 5-day files daily, so each 15-min slot arrives
5-7 times and ~2.6% of overlapping slots are **revised** — sometimes downward
(meter `0050170B`, 2026-05-22T00:00: `0.003` on delivery 20260527 → `0.002` on
20260605). Storage therefore has to express **"the newest delivered value
wins"**, and nothing weaker: keeping every copy makes `sum()` count a slot 5-7
times, and keeping the maximum per slot means a downward revision never lands.

`DEDUP UPSERT KEYS` is genuine last-write-wins — "if the row differs, replaces
the old row" — so overlapping deliveries need nothing more than a plain
`INSERT`. There is no local revision-tracking state, no read-before-write, and
no delete-and-replay.

Revisions are **not** evenly spread, which matters when reading any aggregate.
Across 257 measured day-series, the median day is revised by 0.0000% and 187 of
257 days not at all — but the tail is deep: p90 3.84%, worst +31.24%. Delivery
`20260605` revised 2026-05-27 down 24% (meter `0050170B`'s evening slots fell
from ~1.77 kWh to ~0.002) nine days after the fact, **outside** the normal 5-day
overlap window. Any per-day threshold that separates "deep legitimate revision"
from "lost data" would be separating a number from itself; compare slot *counts*
instead.

## Chronological replay is a correctness requirement

LWW consults **no column** — whatever writes last wins. Replaying delivery
`20260527` after `20260605` silently regresses 4 days, and nothing in the
database will show that it happened (`DEDUP UPSERT KEYS` guarantees exactly one
row per key, so there is no second row to compare against). QuestDB has no
conditional upsert, so ordering is enforced instead of checked:

- **Live ingestion**: safe. The watcher batches by delivery date and flushes on
  date change, so batches are chronological, and files *within* one batch all
  share a delivery date — intra-batch order is irrelevant.
- **Startup rescan**: safe. `sorted(watch_dir.glob("*.xml"))` puts the
  `YYYYMMDD` filename prefix in chronological order.
- **Manual replay**: sort explicitly by delivery prefix. Feeding the archive
  back through the watcher in arbitrary order is the one way to corrupt this
  database.

## Schema

The authoritative DDL is `scripts/questdb_schema.sql`, and
`scripts/questdb_init.py --check-only` verifies the live database against that
file — not against this listing, which is a copy for reading.

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
) TIMESTAMP(ts) PARTITION BY MONTH WAL
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
) TIMESTAMP(ts) PARTITION BY MONTH WAL
DEDUP UPSERT KEYS(ts, direction, segment, product_code, community_id);

CREATE TABLE cel_ingest_log (             -- provenance, ~1 row per file
  ts            TIMESTAMP,
  delivery      SYMBOL,
  file_name     SYMBOL,
  document_type SYMBOL,          -- E66 | E31 | unknown
  rows_written  LONG,
  outcome       SYMBOL           -- ingested | skipped | failed
) TIMESTAMP(ts) PARTITION BY MONTH WAL;
```

`DEDUP` requires WAL and requires the designated timestamp among the keys.

**Two tables, not one with a metric-name column.** A `sum()` can no longer
accidentally mix per-meter readings with the community aggregate that already
contains them. That is structural, not a naming convention.

**No `TTL` clause.** Retention is deliberately left open: the provider's history
is the only copy of this data, and a TTL silently drops the oldest partition.
Add `ALTER TABLE cel_energy SET TTL 2 YEARS` later if the volume ever justifies
it (units `HOUR/DAY/WEEK/MONTH/YEAR`; a bare `2y` also parses). Note TTL is
OSS-only — Enterprise rejects a non-zero `SET TTL` and wants a storage policy.

### Key design rules

**`condition` must never be an upsert key.** The provider revises a slot's grade
across deliveries (estimated → measured). Keyed, one slot becomes **two rows**
and every `sum()` double-counts it. As payload it is properly storable, so
questions like "do these 0.00 kWh readings carry Condition 21?" are plain SQL.

**`code_type` is payload, not a key.** It is functionally dependent on
`product_code` (`2404050010123`/`...124` → `VSENationalCode`, `8716867000030` →
`ebIXCode`). Keyed, a provider-side encoding change would create a phantom
parallel series that silently double-counts instead of overwriting.

**No `project` column.** It would be the constant `'cel'` on every row. The
table name scopes the data.

**No `delivery` column on the data tables.** LWW cannot use it, and it cannot
even *detect* a regression after the fact, since there is only ever one row per
key. Provenance lives in `cel_ingest_log` (~1 row per file) and in the archived
XML filenames rather than being duplicated across ~25M rows. It would also
defeat QuestDB's skip-identical-row optimisation on every overlapping row, since
it changes daily.

### Why `DECIMAL(12,3)`

`DECIMAL(precision, scale)`, precision 1-76, exact fixed point. Storage tiers:
10-18 digits → DECIMAL64 (8 bytes); ≥19 → DECIMAL128. The docs advise keeping
precision ≤18 because DECIMAL64 is faster. `(12,3)` and `(15,3)` are both
DECIMAL64 — identical storage and speed — and 12 allows 999,999,999.999, far
beyond any community total, so extra digits buy nothing.

Two consequences:

- **Scale 3 must match the source exactly.** Source data carries 3 decimals, so
  this is lossless — but a 4-decimal value would be **silently rounded at
  ingest**, so the parser asserts on it instead.
- **Multiplication sums precision:** `DECIMAL(12,3) * DECIMAL(12,3)` →
  `DECIMAL(24,6)`, which promotes to DECIMAL128 and loses the fast path. In
  dashboards multiply by the integer literal `4`, never `4.000m`.

Decimal literals need an `m` suffix (`0.001m`), and QuestDB does **not**
implicitly convert double → decimal, so `WHERE value > 0.5` and `... > 0.5m` are
different comparisons. Aggregate expressions mixing the two will surprise you.

## Ingestion

`Observation.value` is a `decimal.Decimal` parsed from the XML text, never a
`float` — parsing to float first defeats the point of an exact column.

Writes go over **PG-wire (`psycopg`) with `executemany`**: `decimal.Decimal`
maps to `DECIMAL` natively with no suffix handling, and DEDUP applies to
ordinary inserts on WAL tables. ~48k rows per delivery is comfortable.

ILP over HTTP is faster and was considered, but it needs a `d` suffix on decimal
literals (or the value sent as a string for QuestDB to cast), and its Python
client's `Sender.row()` decimal support is unconfirmed — the documented path is
pandas/Arrow. Correctness wins here; the volume does not need the speed.

Either way, the tables must be **created up front**: auto-creation on first
insert would produce a table with no DEDUP and a default `DECIMAL(18,3)`, which
silently reintroduces the double-counting the dedup keys exist to prevent. This
is why `questdb-init` runs to completion before the parser starts.

`scripts/questdb_writer.py` is the whole writer: no local state, no
delete-and-rewrite, just an insert. A failed write marks the file `FAILED` so it
stays in the incoming folder and is retried — never archived having stored
nothing.

## Grafana

Datasource: the official `questdb-questdb-datasource` plugin — **v0.1.8,
pre-1.0, signed `commercial`**. The fallback is Grafana's built-in Postgres
datasource against `questdb:8812`, which the QuestDB docs say works but
configures differently (and has a different macro set, so queries would need
revisiting).

Macros: `$__timeFilter(col)`, `$__sampleByInterval`, `$__fromTime` / `$__toTime`,
`$__conditionalAll(cond, $var)`. There is **no** `$__interval` on this datasource
— that is Grafana/Prometheus naming.

### Four plugin traps, each of which fails silently

No error in the panel, nothing in the Grafana log:

1. **`format` is a numeric enum**, not a string. `src/types.ts`:
   `Format { TIMESERIES = 0, TABLE = 1, AUTO = 2 }`, and `QuestDBSQLQuery`
   requires both `format` and `selectedFormat`. A string like `"time_series"`
   maps to no member and the panel renders "No data". There is **no
   `editorMode` field**, and `meta` accepts only `{timezone, builderOptions}` —
   park anything else at the top level.
2. **No `DECIMAL` converter.** `pkg/converters/converters.go` maps exactly
   `BOOL`, `INT2`, `FLOAT4`, `FLOAT8`, `TIMESTAMP`, `TIMESTAMP_NS`, by exact
   string equality with no pattern fallback; `GetConverter` returns an empty
   `sqlutil.Converter{}` otherwise, so the column arrives as a **string** and
   the panel says "Data is missing a number field". Wrap the output column in
   `cast(... AS DOUBLE)` — the **outer** expression only, so the inner sum stays
   exact. `LONG`/`INT8` is not in the list either, so `count()` needs the same
   treatment.
3. **Frames are named by refId.** The plugin sets `frame.Name = refId`, and
   Grafana prefixes the frame name when a panel has more than one frame — so a
   two-target panel legends as "A From CEL". Worse, that prefix breaks every
   `byName` field override, which then matches nothing and is ignored, dropping
   the panel's colours. Use `byFrameRefID` overrides carrying both `displayName`
   and `color`. Apply it even to single-target panels: `byName` works there
   until a second target is added.
4. **Gauges need `format: TABLE`** and a query returning one row. A bucketed
   query hands the gauge a series it reduces with `lastNotNull`, reporting the
   newest bucket instead of the range total.

### kWh slots vs kW

`sum(value) * 4` is **wrong** for a bucket wider than 15 min (a 1h bucket of
4×1 kWh gives 16, not 4 kW). Average per slot first, then scale:

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

Self-consumption ratios are plain sums:

```sql
SELECT 100 * cast(sum(case when segment = 'cel' then value end) AS DOUBLE)
           / cast(sum(value) AS DOUBLE) AS "CEL % of Consumption"
FROM cel_energy
WHERE $__timeFilter(ts) AND direction = 'consumption'
  AND segment in ('cel', 'grid');
```

`case when ... then value end` with no `else` yields NULL, which `sum()` skips,
so no `0m` literal is needed.

### Dashboard conventions

- **Panels key off `segment`, not `product_code`.** Same distinction, but under
  the name the parser stores, so it survives a provider-side encoding change
  (see `classify_metric_type` in `models.py`). The mapping is
  `2404050010123 → cel`, `2404050010124 → grid`, `8716867000030 → total`.
- **E31 panel 15's difference is one `UNION ALL`** with the E66 side negated, so
  a single `sum()` per slot gives `E31 - Sum(E66)`. `UNION ALL` drops the
  designated timestamp that `SAMPLE BY` needs, so the subquery re-declares it
  with `ORDER BY ts` + `timestamp(ts)`.
- **E31 panels 13-15 read both tables on purpose** — comparing them is the
  point. Everywhere else that would double-count, since `cel_community_energy`
  already contains what `cel_energy` sums to.
  `test_queries_target_the_right_table` holds an explicit `(panel, refId)`
  allow-list.
- **Both sides of those panels filter `community_id`.** Without it the E66 side
  is inflated by the community-less meters below.
- The meter selector matches the variable's 8-char suffix against the full ID
  with `meter_id LIKE '%$meter_id'`. `$__conditionalAll` is the better fit **if**
  the variable is ever set to `includeAll` — it is not today
  (`includeAll: false`, single-select). Switch at the same time as enabling
  "All", not before.

## Known data anomalies

These are provider-side, not query bugs, and every one of them has been
confirmed in the raw XML:

- **8 meters carry no `<Community>` element**, so `community_id` is NULL on
  their rows: `0042214D`, `0042215A`, `0201080P`, `0733915V`, `0854697H`,
  `0854699B`, `0854701T`, `0856898T`. They first appear in delivery `20260729`
  and each carries ~5 months of history back to 2026-02-28. The E31 aggregate
  does not include them, so an **unscoped** `sum(cel_energy)` overstates the
  community by **+23.8% on consumption and +32.9% on production**. Always scope
  by `community_id` when comparing against E31.
- **E31 consumption is all-zero for 2026-06-02..24** (23 days, `cel` and `grid`
  alike, while `production.total` keeps arriving): 960 `Volume` elements, none
  non-zero. Delivered as zeros rather than as absent rows, so no query can
  distinguish it from genuine zero consumption — it simply drags any mean over
  that window down. Exclude the range from consumption comparisons; production
  over it is fine.
- **Per-meter production runs ~10% below the E31 aggregate from 2026-07 on**
  (July −471 kWh, August −102 kWh; May and June match to the decimal). A
  per-meter production reading stopped being delivered while the aggregate kept
  counting it. Meter `0046782G` reports 0.0 production from 2026-07 (1057/1064
  slots zero in July, 184/184 in August) and is a component of this.
- `0858140M` reads zero on 82 of 84 days.

Note that `08552310` has no rows in `cel_energy` at all, by design:
`meter_mappings.yaml` maps virtual `08552310` → physical `0046782G`, and
ingestion attributes the virtual meter's rows to the physical one. They are one
meter.

The E31 panels show the aggregate difference. When it is non-zero,
`toolbox/diagnose_validation_gap.py` breaks it down per day and per meter, which
is what separates "one meter stopped reporting" from "every meter is slightly
off" from "E31 itself is zero".

Open provider questions from this analysis, tracked in `PROVIDER_QUESTIONS.md`:
are the 8 community-less meters members whose `<Community>` element is merely
missing, or genuinely outside the CEL? Why is E31 consumption zero for
2026-06-02..24? Why does per-meter production fall ~10% short from 2026-07 on?

## Security

QuestDB OSS serves the web console and the REST API on **9000 with no meaningful
auth**, and the PG-wire endpoint on 8812 with default credentials
(`admin`/`quest`, unchangeable in OSS without a `server.conf`). Neither port is
published — `docker-compose.yml` deliberately gives the `questdb` service no
`ports:` — so both are reachable only from `cel-network`, where the parser and
Grafana already sit. Publish nothing but Grafana through cloudflared, and do not
expose the console to "just have a look". To inspect it:

```bash
ssh -L 9000:cel-questdb:9000 <nas>     # or docker exec + curl
```

## Operations

`scripts/validate_daily_balance_questdb.py` checks a day's stored sums;
`scripts/validate_daily_balance_sdat.py` computes the same figures straight from
the source XML, so the two together tell you whether a discrepancy is in the
data or in the ingestion. Both must run **inside `cel-parser`** — it is on
`cel-network` and has `psycopg`, and QuestDB's ports are unpublished, so neither
database is reachable from the NAS host shell:

```bash
docker exec -it cel-parser python3 \
    /app/scripts/validate_daily_balance_questdb.py 20260610
```
