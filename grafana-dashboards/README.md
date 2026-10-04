# CEL Grafana Dashboards

Production dashboards for CEL community energy monitoring. All read SQL from
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
- E66 also: `meter_id`, `customer_id` (owner from `config/customers.yaml`; NULL
  for the RCP meters); E31 also: `community_type`, `grid_area`
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

### 1. cel-meter-energy-e66.json
**Meter dashboard** — one metering point, consumption or production. This is the
default home dashboard.

- Meter drop-down listing every community meter, both directions, labelled
  e.g. `0046782G (consumption)`. The query returns one string per meter,
  `<8-char suffix> (<direction>)|<full id>`, and the regex
  `/^(?<text>[^|]+)\|(?<value>.+)$/` splits it with named groups: the label
  is the part before `|`, the value is the full id, so panels match with
  `meter_id = '$meter_id'`.
- A hidden `direction` variable, read from the meter's own rows, for the titles
- Power chart, CEL + Grid stacked (kW)
- Total stat (kWh) and CEL % gauge

A meter stores only its own direction, so the power chart carries two pairs of
queries, consumption (A, B: From CEL / From Grid) and production (C, D: To CEL /
To Grid), and one pair is always empty. That keeps each direction's names and
colours without a query per direction. There is no energy balance: one meter has
nothing to balance against. A customer's meters together, balance included, are
on dashboard 3.

### 2. cel-community-energy-e31.json
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
forking into two rows) or duplicate production-meter totals creeping back in. Both
are prevented at ingest, so a stale gap is cured by a full re-replay through the
current parser — **in ascending delivery order**, since the last write wins.

#### Customers and meters — why production totals aren't double-counted

A customer owns one or more **metering points**, each either **consumption** or
**production** (`085…`). Which customer owns which is declared by the provider,
kept in `config/customers.yaml` — it is nowhere in the XML. Every meter is stored
under its own `meter_id`, with its `customer_id`.

A producing customer's consumption point reports a **copy** of the production
total its production point reports too, so the parser **drops the copy on
ingest**. Net effect: each meter holds only its own direction, and `sum(value)`
over `segment = 'total' AND direction = 'production'` counts each producer once.
`0134575W`'s total has no copy, so it is simply kept.

This is why the meter dashboard shows one direction per meter. The customer
dashboard shows both.

Those dropped files are an expected outcome, not failures: the parser returns a
`SkippedDocument`, the watcher logs it at INFO and archives the file
(`Skipped by design: N` in the batch summary). They are one production-total copy
per consumption point of a producing customer, plus the spurious consumption file
the provider sends for `0134575W`. Only genuine failures stay in `/data/incoming`.

### 3. cel-customer-energy-e66.json
**Customer dashboard** — everything one customer owns, across all their metering
points.

- `customer_id` drop-down
  (`SELECT DISTINCT customer_id FROM cel_energy WHERE customer_id IS NOT NULL`).
  Filtering on it also leaves out the RCP meters, so no `community_id` constant is
  needed.
- Customer summary: consumption (From CEL + From Grid) and production (To CEL +
  To Grid) stacked in kW, total stats, CEL % gauges, energy balance — summed over
  all of the customer's meters
- **Consumption meters** row: one stacked CEL + grid panel per consumption meter
- **Production meters** row: the same per production meter. For a customer that
  does not produce, it shows one empty panel

The per-meter panels repeat over two hidden multi-value variables, `cons_meter`
and `prod_meter` (`SELECT DISTINCT meter_id ... WHERE customer_id = '$customer_id'
AND direction = '...'`). Their regex applies to the **text** only, so the label
is the 8-char suffix and the value stays the full id: panels match with
`meter_id = '$cons_meter'`, not `LIKE`.

The file is in the classic schema like the other two, which has no way to hide
a row whose panels are empty.

## Reference dashboards and experiments

The JSON files here are the **reference** dashboards: versioned in git, provisioned
into the Grafana folder **CEL Reference**, and **locked** (`allowUiUpdates: false`).
A delivery overwrites them, and only them. Each file is named after its uid
(`<uid>.json`); the title shown in Grafana is the JSON `title`, not the file name.

Experiments happen in Grafana, not in git:

1. Open a reference dashboard, then **Save as** (or Settings → Save as copy) into
   the folder **CEL Workspace**. Grafana refuses a plain save on a provisioned
   dashboard, so this is the only way to keep a change.
2. Edit the copy freely. Every save is a version (Settings → Versions), so an
   experiment can be rolled back.
3. To adopt an experiment, export it (Share → Export → JSON), fold the change
   into the reference file **keeping the reference's uid**, commit, and deploy.

What to keep in mind:

- **CEL Workspace is not provisioned, so its dashboards exist only in Grafana's
  database** (`/volume1/docker/cel/grafana-data`). Back that volume up; git does
  not have them.
- **Never put an exported copy into this folder** unless it is meant to become a
  reference: anything here is provisioned, and a copy would show up twice.
- **Copies do not follow the reference.** An improved reference reaches existing
  copies only if the user makes a new copy.

Create the **CEL Workspace** folder once, in the UI (Dashboards → New → New
folder).

`../grafana-workspace/` holds two experiments, changed versions of the meter and
E31 dashboards. They have
their own uids (`<reference uid>-workspace`) and a `(workspace)` title, so they
sit beside the references instead of replacing them. Import each one once into
CEL Workspace (see Manual import); after that they live in Grafana only. That
folder is outside the provisioned path on purpose: the provider reads its
directory recursively.

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
scp cel-meter-energy-e66.json cel-community-energy-e31.json \
    cel-customer-energy-e66.json \
    <nas>:/volume1/docker/cel/grafana-dashboards/
```

> With `disableDeletion: false`, removing a file from disk should delete its
> dashboard; check the Grafana log the first time. A dashboard imported by hand
> was never provisioned, so provisioning never touches it: delete it in the UI.

> The default home dashboard is set in docker-compose via
> `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH` → `cel-meter-energy-e66.json`.
> That env var is read only at container start, so changing it needs a Grafana
> container restart (the dashboards themselves do not).

### Manual import

An imported dashboard is not provisioned, so it is not a reference: import into
**CEL Workspace**.

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
4. It auto-loads into the "CEL Reference" folder within ~10s (no restart).

## More information

See **[PARSING_GUIDE.md](../PARSING_GUIDE.md)** for metric definitions, product
codes (ebIX, VSE), E66 vs E31 file types, and data quality (Condition 21), and
**[QUESTDB.md](../QUESTDB.md)** for the schema, the dedup rules and the full list
of plugin constraints.
