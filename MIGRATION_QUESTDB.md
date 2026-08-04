# Migration: VictoriaMetrics → QuestDB

Status: **phases 0-5 done** -- the schema is live on the NAS, the archive has been
replayed, `questdb.enabled` is `true` there (still `false` in the repo copy of
`config/api_config.yaml`, so do not deploy that file over it), and both
dashboards are fully converted to SQL. VM remains the system of record until
Phase 6 validates the two against each other. Phase table below.

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

That +1.1467% is a **whole-range aggregate**, and the damage is far from evenly
spread. Per day, across 257 day-series measured from the source files: **median
0.0000%, 187 of 257 days exactly 0%, p90 3.84%, worst +31.24%**
(`cel_energy` production 2026-06-04: VM 107.779 kWh vs 82.124 kWh actual). It is
not a per-day expectation and must never be used as a validation tolerance — see
Phase 6.

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
- **Manual replay: sorted explicitly.** `scripts/questdb_replay.py` reads the
  archive's per-delivery `YYYYMMDD.zip` files, sorts them by name (== by date),
  logs the order it will use, and refuses to run if a loose `.xml` in the archive
  is newer than the last zip -- which would otherwise be replayed first and then
  overwritten by an older zip. `--dry-run` prints the order without writing.
  `reprocess_all_data.sh` (the VM-oriented path, which moves files through the
  watcher) relies on the watcher's own sort and carries a header comment saying
  the ordering is now load-bearing.

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
  ts           TIMESTAMP,
  delivery     SYMBOL,
  file_name    SYMBOL,
  document_type SYMBOL,          -- E66 | E31 | unknown
  rows_written LONG,
  outcome      SYMBOL           -- ingested | skipped | failed
) TIMESTAMP(ts) PARTITION BY MONTH WAL;
```

Two tables for E66/E31 rather than one metric-name column: a `sum()` can no
longer accidentally mix per-meter readings with the community aggregate that
already contains them. Structural, not a naming convention.

**No `TTL` clause.** Retention is intentionally left open rather than mirroring
VM's `--retentionPeriod=2y`: the provider's history is the only copy of this data
and a TTL silently drops the oldest partition. Add it later with
`ALTER TABLE cel_energy SET TTL 2 YEARS` if the volume ever justifies it — note
TTL is OSS-only. The authoritative DDL is `scripts/questdb_schema.sql`; this
listing is a copy, and `scripts/questdb_init.py --check-only` verifies the
database against the DDL, not against this document.

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

Status as of 2026-08-03: phases 0-5 complete (197 tests passing). The archive has
been replayed and dual-write is live on the NAS; 6-7 open.

| # | Work | Deliverable | Status |
|---|---|---|---|
| 0 | Add `questdb` service to compose alongside VM | `docker-compose.yml` | done |
| 1 | Schema + idempotent init | `scripts/questdb_schema.sql`, `scripts/questdb_init.py` | done |
| 2 | Writer; `Decimal`-valued observations; watcher writes to QuestDB **and** VM behind a config flag | `scripts/questdb_writer.py`, `watch_ftproot.py`, `config/api_config.yaml` | done (flag off) |
| 3 | Port the test suite to LWW semantics | `tests/test_questdb_writer.py`, `tests/conftest.py` | done (28 new, 99 total) |
| 4 | Replay all archived XML **sorted by delivery prefix** | — (ran via the watcher; `questdb_replay.py` since removed) | done |
| 5 | Convert 27 dashboard expressions to SQL | `grafana-dashboards/*-questdb.json` | done (22 panels, 27 expressions) |
| 6 | Validate QuestDB vs VM day-by-day | — | open |
| 7 | Remove VM, `vm_upsert.py`, `send_to_victoriametrics.py`, VM datasource | — | open |

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

### Phase 5 — dashboard conversion (done)

27 expressions across 22 panels (9/7 in `cel_energy_overview.json`, 18/15 in
`grafana-dashboard-e31-v2.json`), ported to `*-questdb.json` alongside the
originals. Datasource: the official plugin is `questdb-questdb-datasource` —
**v0.1.8, pre-1.0, signed `commercial`**. Fallback is Grafana's built-in Postgres
datasource on port 8812, which the QuestDB docs say works but configures
differently.

`tests/test_dashboards_questdb.py` pins the ports against their originals panel
by panel (only `datasource`, `targets`, `fieldConfig.overrides` and `description`
may differ), so a Phase 6 side-by-side difference can only come from the data.
`tests/test_dashboards.py` covers all four dashboards for defects Grafana does not
report.

#### Plugin constraints that cost real debugging time

Four traps, each of which fails **silently** — no error in the panel, nothing in
the Grafana log:

1. **`format` is a numeric enum**, not a string. `src/types.ts`:
   `Format { TIMESERIES = 0, TABLE = 1, AUTO = 2 }`, and `QuestDBSQLQuery`
   requires both `format` and `selectedFormat`. Writing `"time_series"` (the
   Prometheus/Postgres spelling) maps to no member and the panel renders "No
   data". There is **no `editorMode` field**, and `meta` accepts only
   `{timezone, builderOptions}` — park anything else at the top level.
2. **No `DECIMAL` converter.** `pkg/converters/converters.go` maps exactly
   `BOOL`, `INT2`, `FLOAT4`, `FLOAT8`, `TIMESTAMP`, `TIMESTAMP_NS`, by exact
   string equality with no pattern fallback; `GetConverter` returns an empty
   `sqlutil.Converter{}` otherwise, so the column arrives as a **string** and the
   panel says "Data is missing a number field". Wrap the output column in
   `cast(... AS DOUBLE)` — the **outer** expression only, so the inner sum stays
   exact. `LONG`/`INT8` is not in the list either, so `count()` needs the same
   treatment.
3. **Frames are named by refId.** The plugin sets `frame.Name = refId`, and
   Grafana prefixes the frame name when a panel has more than one frame — so a
   two-target panel legends as "A From CEL". Worse, that prefix breaks every
   `byName` field override, which then matches nothing and is ignored, dropping
   the panel's colours. Use `byFrameRefID` overrides carrying both `displayName`
   and `color`. Apply it even to single-target panels: `byName` works there until
   a second target is added.
4. **Gauges need `format: TABLE`** and a query that returns one row. A bucketed
   query hands the gauge a series it reduces with `lastNotNull`, reporting the
   newest bucket instead of the range total.

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
SELECT 100 * cast(sum(case when segment = 'cel' then value end) AS DOUBLE)
           / cast(sum(value) AS DOUBLE) AS "CEL % of Consumption"
FROM cel_energy
WHERE $__timeFilter(ts) AND direction = 'consumption'
  AND segment in ('cel', 'grid');
```

**This is a deliberate behaviour change, not a translation.** `increase()` treats
its input as a counter, but `cel_energy_kwh` is kWh-per-15-min-slot and falls as
well as rises, so every decrease read as a counter reset and the VM gauges
computed their ratio from accumulated positive deltas rather than from the
totals. The four gauges (overview 5-6, E31 4-5) will therefore **disagree with
their VM counterparts** — expected, and not a Phase 6 finding. `case when ... then
value end` with no `else` yields NULL, which `sum()` skips, so no `0m` literal is
needed.

Three further notes on the port, each flagged in the panel descriptions too:

- **E31 keys off `segment`, not `product_code`.** Same distinction, but under the
  name the parser stores, so it survives a provider-side encoding change (see
  `classify_metric_type` in `models.py`). The mapping is
  `2404050010123 → cel`, `2404050010124 → grid`, `8716867000030 → total`.
- **Panel 15's difference is one `UNION ALL`** with the E66 side negated, so a
  single `sum()` per slot gives `E31 - Sum(E66)`. `UNION ALL` drops the designated
  timestamp that `SAMPLE BY` needs, so the subquery re-declares it with
  `ORDER BY ts` + `timestamp(ts)`.
- **Panels 13-15 read both tables on purpose** — comparing them is the point.
  Everywhere else that would double-count, since `cel_community_energy` already
  contains what `cel_energy` sums to; `test_queries_target_the_right_table` holds
  an explicit `(panel, refId)` allow-list.
- **Panels 13-15 add `community_id = '101110-002726'` to the `cel_energy` side**,
  which the VM original did **not** have. This is a fixed defect, not a
  translation — see "The validation gap was the panels, not the data" below.

#### The validation gap was the panels, not the data

The E31 validation panels showed `Sum(E66)` running well above the E31 aggregate
(consumption 8.14 vs 6.32 kW, production 24.6 vs 20.1). Reproduced on 4368 real
delivery files through the actual ingestion path (real mappings, `SkippedDocument`
drops, breakdown attribution, LWW dedup) — the cause is entirely in the query:

The provider delivers E66 files for **8 meters with no `<Community>` element**, so
`community_id` is NULL on their rows: `0042214D`, `0042215A`, `0201080P`,
`0733915V`, `0854697H`, `0854699B`, `0854701T`, `0856898T`. They first appear in
delivery 20260729 and each carries ~5 months of history back to 2026-02-28. The
E31 aggregate does not include them, but the panel's `sum(cel_energy)` did:
**+23.8% on consumption, +32.9% on production**, which is the whole gap.

With the filter, on days where E31 has data, the two sides agree to within ~1.5-3%
— ordinary revision noise, except for two real residuals:

- **Production, 2026-07 onward: E66 reads ~10% *low*** (July −471 kWh, August
  −102 kWh; May and June match to the decimal). A per-meter production reading
  stopped being delivered while the E31 aggregate kept counting it. Worth a
  provider question — it is a data problem, not a query problem.
- **E31 consumption is all-zero for 2026-06-02..24** (23 days, `cel` and `grid`
  too, while `production.total` keeps arriving). Confirmed in the raw XML: 960
  `Volume` elements, none non-zero. A provider-side gap, delivered as zeros rather
  than as absent rows — so a query cannot distinguish it from genuine zero
  consumption and it drags any mean over that range down.

The two meters originally suspected, `0046782G` and `08552310`, explain none of
it. They are **one meter**: `meter_mappings.yaml` maps virtual `08552310` →
physical `0046782G`, and the ingestion attributes the virtual meter's rows to the
physical one, so `08552310` has no rows in `cel_energy` at all. `0046782G` does
report 0.0 production from 2026-07 on (1057/1064 slots zero in July, 184/184 in
August) — but a zero can only pull `Sum(E66)` *down*, and the gap was upward. It
is a component of the ~10% production shortfall above, not of the headline gap.

Plugin macros are `$__timeFilter(col)`, `$__sampleByInterval`, `$__fromTime` /
`$__toTime`, and `$__conditionalAll(cond, $var)`. There is **no** `$__interval`
macro on this datasource (that is Grafana/Prometheus naming); the Postgres
fallback has a different set again, so macros must be revisited if you switch
datasources.

The overview's `meter_id=~".*${meter_id}"` regex became
`meter_id LIKE '%$meter_id'`, matching the variable's 8-char suffix against the
full ID as the original did. `$__conditionalAll` is the better fit **if** the
variable is ever set to `includeAll` — it is not today (`includeAll: false`,
single-select), so `LIKE` matches the current behaviour exactly. Switch to
`$__conditionalAll` at the same time as enabling "All", not before.

Bonus: `toolbox/diagnose_validation_gap.py` (a ~500-line Python E66-vs-E31
reconciliation) becomes a SQL join and can move into a dashboard panel.

### Phase 6 — validation

**Tooling (done, not yet run against the live databases):**

- `scripts/compare_vm_questdb.py` — the per-day VM-vs-QuestDB comparison.
  `--anomalies` runs the known-anomaly check on its own.
- `scripts/validate_daily_balance_questdb.py` — the QuestDB counterpart of
  `validate_daily_balance_vm.py`, which it replaces in Phase 7.
- `tests/test_compare_vm_questdb.py` — 26 tests on the verdict logic.

Neither script can run from a dev checkout: both databases are on the NAS. Copy
both into `/volume1/docker/cel/scripts/`, then run them **inside `cel-parser`** —
it is on `cel-network`, already has `psycopg` and `VICTORIA_METRICS_URL`, and
QuestDB's 8812/9000 are deliberately unpublished, so neither database is reachable
from the NAS host shell:

```bash
docker exec -it cel-parser python3 /app/scripts/compare_vm_questdb.py \
    20260430 20260727 --json /app/logs/phase6.json ; echo "exit=$?"
docker exec -it cel-parser python3 /app/scripts/compare_vm_questdb.py \
    20260430 20260727 --anomalies
docker exec -it cel-parser python3 \
    /app/scripts/validate_daily_balance_questdb.py 20260610
```

Exit codes: 0 green, 1 a day outside expectation, 2 nothing compared (an empty
range reads as a clean report otherwise), 3 a query failed.

Read the report rather than only the exit code: `OK_DEEP_REVISION` rows pass by
design and are the ones worth eyeballing. Then check the `20260803` gap below.

#### There is no expected percentage — this was wrong

This plan said to expect QuestDB **~1.15% lower** than VM per day and to treat a
larger gap as suspicious. **That is wrong**, and the check built on it would have
failed ~14 legitimate days. Measured by replaying 4368 real delivery files through
both dedup rules (`max()` for VM, last-write for QuestDB):

| series | aggregate gap | per-day min | per-day median |
|---|---|---|---|
| `cel_energy` consumption | −3.09% | 0.764 | **1.00000** |
| `cel_energy` production | −0.58% | 0.762 | **1.00000** |
| `cel_community_energy` consumption | −2.31% | 0.765 | **1.00000** |
| `cel_community_energy` production | −0.21% | 0.762 | **1.00000** |

The per-day ratio is **bimodal**, not centred on 1.15%: most days are *exactly*
1.00000, and a minority run as low as 0.762. Revisions are not spread evenly — a
day is untouched until the provider re-issues it, and then it moves a lot.
Delivery **20260605 revised 2026-05-27 down 24%**: meter `0050170B`'s evening
slots went from ~1.77 kWh to ~0.002, nine days after the fact and **outside the
normal 5-day overlap window**. VM is stuck at 1.77 forever; QuestDB holds 0.002.

So no threshold separates "deep legitimate revision" from "lost data" — they are
the same number. The comparison therefore judges only what holds regardless of
revision depth:

1. **QuestDB must never exceed VM.** `max(samples) >= last(sample)` on identical
   input, so an excess is provably an ingest divergence. No tolerance.
2. **Both must hold the same slot count.** Exact comparison. This is what catches
   missing data, and it catches it *without* a value threshold — which is why the
   magnitude does not need one. A day legitimately 24% lower still has all its
   slots; a day that lost rows does not.

The magnitude is reported and summed for the record but cannot fail the run. Days
at or below 0.95 are labelled `OK_DEEP_REVISION` — they pass, and are surfaced so
they get read rather than buried in ~280 rows.

**This also means the "+1.1467% inflation" figure quoted under "Why" is a range
aggregate, not a per-day expectation.** Do not use it as a tolerance.

Also confirm the known anomalies survive (`--anomalies`): meter `0046782G`
producing 0.0 from 2026-06-23, `0858140M` zero on 82/84 days. An anomaly that
*vanished* is data loss, so the check fails on zero rows as well as on a changed
value — a sum alone reads 0.0 for both "still zero" and "gone".

Exclude **2026-06-02..24** from any consumption comparison: E31 consumption is
zero across those days at source (see Phase 5), so both databases will agree on a
wrong number and the range's mean is not meaningful. The script skips them
automatically, for consumption only — production over that window is fine and is
still compared.

Compare **energy panels only**. The four gauges (overview 5-6, E31 4-5) are
expected to disagree for a reason unrelated to dedup: dropping `increase()` was a
correction rather than a translation — see Phase 5. Comparing them would produce a
large difference that has nothing to do with what is being measured.

One known gap to check first: `20260803_094734_...E31....xml` failed its QuestDB
write (a dropped connection, since fixed in `questdb_writer.py`) while VM accepted
it. With `questdb.required: false` it was logged, archived, and not retried, and
its `cel_ingest_log` row is missing too — the failed-outcome write used the same
dead connection. The provider's overlapping 5-day deliveries should re-cover those
slots, so verify rather than assume.

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
  the string-cast route required? (Phase 2 — moot, we took the PG-wire path)
- Does any source file ever carry more than 3 decimals? (Phase 2 assertion)
- Whether the four gauges should keep the corrected ratio or reproduce VM's
  `increase()` behaviour for continuity. The correction is live; the VM panels are
  still there to compare against until Phase 7.
- **Provider questions raised by the validation analysis** (add to
  `config/provider_questions` work): are the 8 community-less meters
  (`0042214D`, `0042215A`, `0201080P`, `0733915V`, `0854697H`, `0854699B`,
  `0854701T`, `0856898T`) members whose `<Community>` element is simply missing,
  or genuinely outside the CEL? Why is E31 consumption zero for 2026-06-02..24?
  And why does per-meter production fall ~10% short of the E31 aggregate from
  2026-07 on?
- The **VM dashboard `grafana-dashboard-e31-v2.json` still has the unscoped
  `sum(cel_energy_kwh{...})`** in panels 13-15. Left alone deliberately: it is the
  comparison baseline until Phase 7, when it is deleted anyway. Its validation
  panels will keep showing the inflated Sum(E66) until then.

Resolved in Phase 5: the Grafana plugin v0.1.8 is viable — no need for the
Postgres fallback — subject to the four silent-failure constraints listed under
Phase 5 (numeric `format` enum, no `DECIMAL` converter, refId-prefixed frame
names, `TABLE` format for gauges).

Resolved while writing this guide: `DEDUP UPSERT KEYS` is last-write-wins and
requires WAL + the designated timestamp in the keys; `DECIMAL` exists in OSS with
precision 1-76; TTL is OSS-only (**Enterprise rejects non-zero `SET TTL`** and
requires a storage policy instead — relevant only if this ever moves to
Enterprise); `TTL 2 YEARS` is valid (units `HOUR/DAY/WEEK/MONTH/YEAR`, so a bare
`2y` is also accepted); the Grafana macro is `$__sampleByInterval`.
