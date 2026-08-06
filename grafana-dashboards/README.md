# CEL Grafana Dashboards

Production dashboards for CEL community energy monitoring. Both read SQL from
QuestDB through the `questdb-questdb-datasource` plugin.

## Table & column schema

Two tables, each split by orthogonal columns rather than baked-into-the-name
variants:

| Table | Source | Meaning |
|-------|--------|---------|
| `cel_energy` | E66 | Per-meter energy, kWh per 15-min interval |
| `cel_community_energy` | E31 | Community aggregate, kWh per 15-min interval |

Shared columns:

- `direction` = `consumption` \| `production`
- `segment` = `cel` \| `grid` \| `total`  (total = cel + grid)
- `product_code` (`8716867000030` = total, `2404050010123` = CEL, `2404050010124` = grid), `code_type`, `community_id`
- E66 also: `meter_id`; E31 also: `community_type`, `grid_area`
- `value` is `DECIMAL(12,3)`

> **Panels filter on `segment`, not `product_code`.** They are the same
> distinction, but `segment` is the name the parser derives and stores, so a
> provider-side change of encoding does not silently empty a panel.

> **`condition` is stored but is never part of a row's identity.** The provider
> marks each reading measured or estimated (`21`) and *revises that grade across
> overlapping deliveries* — the same slot can arrive estimated one day and
> measured the next. If `condition` were a dedup key, that slot would become two
> rows and every `sum()` would double-count it. It is payload, so each
> `(ts, meter, segment, direction, product_code, community_id)` is one row and a
> later delivery overwrites it in place.

> Two tables are kept deliberately so `sum()` over per-meter data cannot be
> confused with the community aggregate that already contains it.

## Two plugin rules every query obeys

Both fail **silently** — the panel renders empty or as a string, with nothing in
the Grafana log. Full list in [../QUESTDB.md](../QUESTDB.md#four-plugin-traps-each-of-which-fails-silently).

1. **Every output column is `cast(... AS DOUBLE)`.** The plugin has no `DECIMAL`
   converter, so an un-cast `value` arrives as a string and the panel reports
   "Data is missing a number field". Cast the **outer** expression only, so the
   inner `sum()` stays exact.
2. **Series are named with `byFrameRefID` overrides, never `byName`.** The plugin
   names each frame after its refId and Grafana prefixes multi-frame panels with
   it ("A From CEL"), which makes a `byName` matcher match nothing — taking the
   panel's colours down with it.

## Power vs energy on line charts

Stored values are **energy per 15-min interval** (kWh/15min), which is additive.
Plotted raw, Grafana's downsampling picks one sample per step instead of summing,
so a zoomed-out chart under-reads by up to ~4×.

The timeseries (line) panels therefore display **average power in kW**:

```sql
SELECT ts AS time, cast(avg(slot_kwh) * 4 AS DOUBLE) AS "..."
FROM (
  SELECT ts, sum(value) AS slot_kwh
  FROM cel_energy
  WHERE $__timeFilter(ts) AND ...
  SAMPLE BY 15m           -- pins the slot width, whatever the outer bucket is
)
SAMPLE BY $__sampleByInterval FILL(NULL)
ORDER BY time
```

- The inner `SAMPLE BY 15m` sums across meters within one native slot; the outer
  `avg` then averages slots inside the display bucket, so energy is conserved at
  any zoom level.
- `* 4` converts kWh per 15-min (0.25 h) to kW. Note this multiplies by an
  **integer** literal, never `4.000m`: decimal × decimal sums precision and would
  push the column out of the DECIMAL64 fast path.
- **`sum(value) * 4` is wrong** for any bucket wider than 15 min — a 1h bucket of
  4×1 kWh gives 16, not 4 kW. Average first, then scale.
- Unit is `kwatt` (kW).

Stat and pie panels don't need the conversion — their panel-level reduce `sum`
totals energy over the range correctly. Gauges use plain `sum(value)` ratios and
`format: TABLE`, because a bucketed query would be reduced with `lastNotNull` and
report the newest bucket instead of the range total.

### Legend calcs on power charts — `Mean` and `Max`

Every timeseries panel's legend shows `calcs: ["mean", "max"]`. The three
**validation** panels add `min`: their series are signed differences, so the
negative extreme matters as much as the positive one and `max` alone would hide
it.

A Grafana timeseries panel applies **one unit to the whole field**, so all legend
calcs are formatted with that unit and there is no per-calc unit. On a power (kW)
line, `Mean` and `Max` are meaningful (average / peak power), but `Total` is
`Σ(power samples)` — meaningless as kW, ~4× the energy at 15-min resolution, and
not stable across zoom. So **`Total`/`Sum` is not shown on any power chart**.

Energy (kWh) is not mixed into these legends either. Read it from the panels
built for it: the **stat** panels (Total Consumption / Total Production) and the
**pie** panels, whose panel-level reduce `sum` totals true energy over the range.

> An earlier version added a hidden `... (kWh)` companion series pinned to
> `unit: kwatth` + `hideFrom.viz` so the legend could carry real energy. It was
> removed: two entries per segment made the legend harder to read for a number
> already available elsewhere. Don't reintroduce it — if a query or override
> mentions `(kWh)`, it's a leftover.

## Available Dashboards

### 1. cel_energy_overview.json
**Individual meter dashboard** — per-meter consumption and production. This is
the default home dashboard.

- Meter selector dropdown, populated by
  `SELECT DISTINCT meter_id FROM cel_energy WHERE segment = 'total' AND direction = 'consumption' ORDER BY meter_id`,
  with `regex: .*([0-9A-Z]{8})$` reducing each ID to its 8-char suffix
- Daily consumption / production charts, CEL + Grid split (kW)
- Total consumption / production stats (kWh)
- CEL % gauges (share of consumption / production from CEL)
- Energy balance line chart (net production − consumption, kW)

Reads `cel_energy`, filtered by `segment` / `direction` / `meter_id`. The meter
filter is `meter_id LIKE '%$meter_id'`, matching the variable's 8-char suffix
against the full ID. If the variable is ever switched to `includeAll`, change
these to `$__conditionalAll` at the same time — not before, since `LIKE` is what
matches today's single-select behaviour exactly.

### 2. grafana-dashboard-e31-v2.json
**Community aggregate dashboard** — community-level totals and statistics.

- Total community consumption / production (kWh)
- Self-sufficiency rate (CEL %) and grid dependency (%) gauges
- Consumption / production over time — **CEL + Grid only** (total omitted for clarity), kW
- Consumption / production source distribution (pie, CEL vs Grid)
- Validation: E31 aggregate vs sum of E66 `segment = 'total'` meters
  (measured-vs-measured), plus a difference panel that should sit near zero

Reads `cel_community_energy`, filtered by `direction` / `segment`.

#### What the validation panels should show

Panels 13-15 read **both tables on purpose** — comparing them is the point.
Everywhere else that would double-count, so
`test_queries_target_the_right_table` holds an explicit `(panel, refId)`
allow-list.

**Both sides filter `community_id = '101110-002726'`, and that filter is
load-bearing.** The provider delivers E66 files for 8 meters with no `<Community>`
element, so their `community_id` is NULL and they are absent from the E31
aggregate. An unscoped `sum(cel_energy)` includes them and overstates the E66
side by **~24% on consumption and ~33% on production** — which looks exactly like
a validation failure and is not one.

Panel 15 computes the difference with one `UNION ALL` and the E66 side negated,
so a single `sum()` per slot yields `E31 − Sum(E66)`. `UNION ALL` drops the
designated timestamp that `SAMPLE BY` needs, hence the subquery's `ORDER BY ts` +
`timestamp(ts)`.

With the community filter in place, on days where E31 has data the two sides
agree to within ~1.5-3% — ordinary revision noise — except for these known
provider-side residuals:

- **Production from 2026-07 onward: E66 reads ~10% low** (July −471 kWh, August
  −102 kWh; May and June match to the decimal). A per-meter production reading
  stopped being delivered while the E31 aggregate kept counting it. Meter
  `0046782G` reports `0.000` production from 2026-07 (1057/1064 slots zero in
  July, 184/184 in August) and is a component of this. See
  `PROVIDER_QUESTIONS.md` Q16c.
- **E31 consumption is all-zero for 2026-06-02..24** — 23 days, `cel` and `grid`
  alike, while `production.total` keeps arriving. Confirmed in the raw XML: 960
  `Volume` elements, none non-zero. Delivered as zeros rather than as absent
  rows, so no query can tell it from genuine zero consumption; it simply drags
  any mean over that window down. The difference panel will show a large gap
  there and it is not an ingest problem.
- A monthly E31 file injects consumption for 2026-04-30 where no E66 exists, so
  that day alone reads as a large negative difference. See
  `PROVIDER_QUESTIONS.md` Q16a/Q16b.

A gap of the *wrong sign* on production — sum(E66) ≈ 1.6× E31 — would instead
mean an ingest regression: `condition` promoted to a dedup key (revised slots
forking into two rows) or duplicate virtual-meter totals creeping back in. Both
are prevented at ingest, so a stale gap is cured by a full re-replay through the
current parser — **in ascending delivery order**, since the last write wins.

#### Meter attribution — why production totals aren't double-counted

Each producer has a **physical** meter and a **virtual** (`085…`) meter. The
virtual meter reports a production **total identical** to the physical meter's
(that equality is how the two are paired during discovery). The parser therefore
**drops the virtual meter's total on ingest** and keeps only the physical one,
while re-attributing the virtual meter's CEL/Grid **breakdown** to the physical
`meter_id`. Net effect: every producer's consumption *and* production live under
one physical ID, and `sum(value)` over
`segment = 'total' AND direction = 'production'` counts each producer once. (The
self-contained meter `0134575W` carries its own total + breakdown and is exempt
from this drop.)

Those ~9 dropped files per delivery are an expected outcome, not failures: the
parser returns a `SkippedDocument`, the watcher logs it at INFO and archives the
file (`Skipped by design:` / `Skipped by design: 9` in the batch summary). Only
genuine failures stay in `/data/incoming`.

## Installation

### Automatic (provisioning)

Dashboards in this directory are mounted into Grafana via docker-compose:

```yaml
volumes:
  - /volume1/docker/cel/grafana-dashboards:/var/lib/grafana/dashboards
```

The provider (`grafana-provisioning/dashboards/dashboards.yaml`) sets
`updateIntervalSeconds: 10`, so Grafana **re-reads the JSON from disk every ~10s**.
Editing a file on the mounted path is enough — no restart or API reload needed.

To deploy an edit, copy the file to the NAS path:

```bash
scp cel_energy_overview.json grafana-dashboard-e31-v2.json \
    <nas>:/volume1/docker/cel/grafana-dashboards/
```

> Provisioning adds and updates dashboards but does **not** delete ones removed
> from disk. A dashboard that has been renamed or retired must be deleted by hand
> in the Grafana UI, or it lingers as a stale copy.

> The default home dashboard is set in docker-compose via
> `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH` → `cel_energy_overview.json`.
> That env var is read only at container start, so changing it needs a Grafana
> container restart (the dashboards themselves do not).

### Manual import

1. Open Grafana: https://grafana.oche22.ch (or `http://<synology-ip>:3000`)
2. Log in
3. Dashboards → Import → upload JSON
4. Select datasource: **QuestDB**

## Queries

All queries use the QuestDB datasource, provisioned in
`grafana-provisioning/datasources/questdb.yaml` against `questdb:8812` (PG-wire).
Available macros: `$__timeFilter(col)`, `$__sampleByInterval`, `$__fromTime` /
`$__toTime`, `$__conditionalAll(cond, $var)`. There is **no** `$__interval` or
`$__range` on this datasource.

```sql
-- Per-meter consumption as power (kW)
SELECT ts AS time, cast(avg(slot_kwh) * 4 AS DOUBLE) AS "From CEL"
FROM (
  SELECT ts, sum(value) AS slot_kwh
  FROM cel_energy
  WHERE $__timeFilter(ts) AND segment = 'cel' AND direction = 'consumption'
    AND meter_id LIKE '%$meter_id'
  SAMPLE BY 15m
)
SAMPLE BY $__sampleByInterval FILL(NULL)
ORDER BY time;

-- Community CEL-local consumption as power (kW)
SELECT ts AS time, cast(avg(slot_kwh) * 4 AS DOUBLE) AS "From CEL"
FROM (
  SELECT ts, sum(value) AS slot_kwh
  FROM cel_community_energy
  WHERE $__timeFilter(ts) AND community_id = '101110-002726'
    AND segment = 'cel' AND direction = 'consumption'
  SAMPLE BY 15m
)
SAMPLE BY $__sampleByInterval FILL(NULL)
ORDER BY time;

-- Share of consumption from CEL over the range (%) -- gauge, format: TABLE
SELECT 100 * cast(sum(case when segment = 'cel' then value end) AS DOUBLE)
           / cast(sum(value) AS DOUBLE) AS "CEL % of Consumption"
FROM cel_energy
WHERE $__timeFilter(ts) AND direction = 'consumption'
  AND segment in ('cel', 'grid');
```

`case when ... then value end` with no `else` yields NULL, which `sum()` skips —
so no `0m` literal is needed. Note that a bare `0.5` would **not** compare
against a `DECIMAL(12,3)` column the way you expect: QuestDB does not implicitly
convert double → decimal, so decimal literals need the `m` suffix (`0.5m`).

## Troubleshooting

QuestDB's ports are not published, so every check below runs from inside a
container on `cel-network`.

**No data displayed:**
1. Check the parser is ingesting: `docker logs cel-parser`
2. Verify rows exist:
   `docker exec cel-parser python3 -c "import psycopg,os; print(psycopg.connect(os.environ['QUESTDB_DSN']).execute('SELECT count() FROM cel_energy').fetchone())"`
3. Check the schema was applied: `docker logs cel-questdb-init`

**Panel says "Data is missing a number field":** a `DECIMAL` column reached
Grafana un-cast. Wrap the output column in `cast(... AS DOUBLE)`.

**Panel renders empty with no error:** most often `format` was written as a
string. It is a numeric enum — `0` for timeseries, `1` for table — and both
`format` and `selectedFormat` must be set.

**Legend shows "A <name>" and the colours are gone:** the panel has more than one
frame and its overrides use `byName`. Switch them to `byFrameRefID`.

**Meter selector empty:** no `cel_energy` rows yet — process or replay files
first (in ascending delivery order).

**Wrong datasource:** Connections → Data sources → verify **QuestDB** points at
`questdb:8812`.

## Adding new dashboards

1. Create the dashboard JSON.
2. Add its filename to `DASHBOARD_FILES` in `tests/test_dashboards_sql.py` — the
   suite asserts every JSON in this folder is listed, so an unlisted one fails
   rather than going untested.
3. Copy it to `/volume1/docker/cel/grafana-dashboards/`.
4. It auto-loads into the "CEL" folder within ~10s (no restart).

## More information

See **[PARSING_GUIDE.md](../PARSING_GUIDE.md)** for metric definitions, product
codes (ebIX, VSE), E66 vs E31 file types, and data quality (Condition 21), and
**[QUESTDB.md](../QUESTDB.md)** for the schema, the dedup rules and the full list
of plugin constraints.
