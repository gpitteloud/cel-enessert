# CEL Grafana Dashboards

Production dashboards for CEL community energy monitoring.

## Metric & label schema

Both dashboards use two energy metrics, each split by orthogonal labels rather
than baked-into-the-name variants:

| Metric | Source | Meaning |
|--------|--------|---------|
| `cel_energy_kwh` | E66 | Per-meter energy, kWh per 15-min interval |
| `cel_community_energy_kwh` | E31 | Community aggregate, kWh per 15-min interval |

Shared labels:

- `direction` = `consumption` \| `production`
- `segment` = `cel` \| `grid` \| `total`  (total = cel + grid)
- `product_code` (`8716867000030` = total, `2404050010123` = CEL, `2404050010124` = grid), `code_type`, `community_id`
- E66 also: `meter_id`; E31 also: `community_type`, `grid_area`

> **`condition` is NOT a label.** The provider marks each reading measured or
> estimated (`21`) and *revises that grade across overlapping deliveries* — the
> same 15-min slot can arrive estimated one day and measured the next. If
> `condition` were part of the series identity, that slot would land in two
> parallel series and every `sum()` would double-count it. So the parser drops
> `condition` on ingest: each `(meter, segment, direction)` is a single series
> and a later delivery overwrites the earlier value in place. Queries therefore
> use bare selectors — no `sum without(condition)` wrapper is needed anywhere.

> Two distinct metric names are kept deliberately so `sum()` over per-meter data
> is never confused with the community aggregate.

## Power vs energy on line charts

Stored samples are **energy per 15-min interval** (kWh/15min), which is additive.
If plotted raw, Grafana's downsampling picks one sample per step instead of
summing, so a zoomed-out chart under-reads by up to ~4×.

The timeseries (line) panels therefore display **average power in kW** instead:

```promql
(avg_over_time(<selector>[$__interval:15m])) * 4
```

- `avg_over_time(...[$__interval:15m])` averages every 15-min sample inside the
  step bucket, so energy is conserved at any zoom level.
- `* 4` converts kWh per 15-min (0.25 h) to kW.
- The `:15m` subquery step floors the resolution at the native 15 min so
  `$__interval` never drops below it (no empty buckets / gaps).
- Unit is `kwatt` (kW).

Stat and pie panels don't need the power conversion — their panel-level reduce
`sum` totals energy over the range correctly — so they use bare selectors.
Gauge panels use `sum(increase(<selector>[$__range]))`.

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

> An earlier version added a hidden `... (kWh)` companion series (a
> `sum_over_time(...)` target pinned to `unit: kwatth` + `hideFrom.viz`) so the
> legend could carry real energy. It was removed: two entries per segment made
> the legend harder to read for a number already available elsewhere. Don't
> reintroduce it — if a query or override mentions `(kWh)`, it's a leftover.

## Available Dashboards

### 1. cel_energy_overview.json
**Individual meter dashboard** — per-meter consumption and production.

- Meter selector dropdown (auto-populated from `label_values(cel_energy_kwh{segment="total", direction="consumption"}, meter_id)`)
- Daily consumption / production charts, CEL + Grid split (kW)
- Total consumption / production stats (kWh)
- CEL % gauges (share of consumption / production from CEL)
- Energy balance line chart (net production − consumption, kW)

Metric: `cel_energy_kwh`, filtered by `segment` / `direction` / `meter_id`.

### 2. grafana-dashboard-e31-v2.json
**Community aggregate dashboard** — community-level totals and statistics.

- Total community consumption / production (kWh)
- Self-sufficiency rate (CEL %) and grid dependency (%) gauges
- Consumption / production over time — **CEL + Grid only** (total omitted for clarity), kW
- Consumption / production source distribution (pie, CEL vs Grid)
- Validation: E31 aggregate vs sum of E66 `segment="total"` meters (measured-vs-measured), plus a difference panel that should sit near zero

Metric: `cel_community_energy_kwh`, filtered by `direction` / `product_code`.

#### What the validation panels should show

The E66 side sums `segment="total"` across all meters. This is a clean
measured-vs-measured comparison **only because production totals are
single-sourced** — see below.

- **Production:** the difference panel sits at **≈ 0 up to data date
  2026-06-30** (E31 total == sum of E66 physical production totals, exact to
  rounding, e.g. both 2558.6 kWh on 2026-06-15). A gap of the *wrong sign* —
  sum(E66) ≈ 1.6× E31 — means an ingest regression: an old parser storing
  `condition` as a label (revised slots double-counted) or duplicate
  virtual-meter totals creeping back in, see below. Both are fixed at ingest, so
  a stale gap is cured by a full re-replay through the current parser.
- **Production from 2026-07-01:** sum(E66) is a steady **0.89–0.94 × E31**
  (30–60 kWh/day short) for a provider-side reason, not a bug. Meter `0046782G`
  delivers production `0.000` for every interval from data date 2026-06-23
  onward — on its physical ebIX total *and* on virtual twin `08552310`'s
  CEL/Grid breakdown — yet E31 started including it again on 2026-07-01. The
  missing energy has the shape of one meter's daily solar profile. See
  `PROVIDER_QUESTIONS.md` Q16c.
- **Consumption:** small and positive — sum(E66) runs **1.00–1.09 × E31**
  throughout (E66 slightly above E31, no sign flip). A monthly E31 file also
  injects consumption from 2026-04-30 where no E66 exists — that day alone reads
  as a large negative difference. See `PROVIDER_QUESTIONS.md` Q16a/Q16b.

> **If June consumption shows E31 ≈ 0 (sum(E66) > 2× E31), the monthly backfill
> lost the overwrite race.** June 2026 E31 consumption exists in exactly **one
> place**: six monthly E31 files (2880 observations each, covering
> 2026-05-31..2026-06-30) delivered on **2026-07-07** at `1047xx`. Every June
> *daily* delivery (20260603..20260625) carries `0.000` for all consumption
> intervals. The backfill and the dailies produce **identical label sets**, so
> they are the same VM series and the **last write wins**. Replayed in ascending
> delivery order the backfill lands last and June reads 1.02–1.09; if those six
> files are re-ingested *before* the June dailies — or never ingested at all
> (e.g. dedup skipped them because they were already listed in an archive zip) —
> June collapses to E31 = 0 for 22 days.
>
> Fix without a full re-replay: re-ingest just those six files **after**
> everything else, then confirm June ratios return to ~1.03.

#### Meter attribution — why production totals aren't double-counted

Each producer has a **physical** meter and a **virtual** (`085…`) meter. The
virtual meter reports a production **total identical** to the physical meter's
(that equality is how the two are paired during discovery). The parser therefore
**drops the virtual meter's total on ingest** and keeps only the physical one,
while re-attributing the virtual meter's CEL/Grid **breakdown** to the physical
`meter_id`. Net effect: every producer's consumption *and* production live under
one physical ID, and `sum(cel_energy_kwh{segment="total", direction="production"})`
counts each producer once. (The self-contained meter `0134575W` carries its own
total + breakdown and is exempt from this drop.)

Those ~9 dropped files per delivery are an expected outcome, not failures: the
parser returns a `SkippedDocument`, the watcher logs it at INFO and archives the
file (`Skipped by design:` / `Skipped by design: 9` in the batch summary). Only
genuine failures stay in `/data/incoming`.

> If the production validation panel ever shows sum(E66) ≈ 1.6× E31, the fix is
> either a stale replay (run `scripts/cleanup_virtual_production_totals.py`) or a
> regression in the `parse_e66` virtual-total drop.

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

> The default home dashboard is set in docker-compose via
> `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH` → `cel_energy_overview.json`.
> That env var is read only at container start, so changing it needs a Grafana
> container restart (the dashboards themselves do not).

### Manual import

1. Open Grafana: https://grafana.oche22.ch (or `http://<synology-ip>:3000`)
2. Login: admin/admin
3. Dashboards → Import → upload JSON
4. Select datasource: **VictoriaMetrics**

## Queries

All queries use the VictoriaMetrics datasource at `http://victoriametrics:8428`.

```promql
# Per-meter consumption as power (kW)
(avg_over_time(cel_energy_kwh{segment="total", direction="consumption", meter_id=~".*${meter_id}"}[$__interval])) * 4

# Community CEL-local consumption as power (kW)
(avg_over_time(cel_community_energy_kwh{community_id="101110-002726", product_code="2404050010123", direction="consumption"}[$__interval])) * 4

# Total energy over a range (kWh) — stat/gauge style
sum(increase(cel_energy_kwh{segment="total", direction="consumption"}[$__range]))
```

## Troubleshooting

**No data displayed:**
1. Check VictoriaMetrics: `curl http://localhost:8428/health`
2. Verify series exist: `curl 'http://localhost:8428/api/v1/series?match[]=cel_energy_kwh'`
3. Check parser logs: `docker logs cel-parser`

**Meter selector empty:** no `cel_energy_kwh` data in VictoriaMetrics yet — process/replay files first.

**Wrong datasource:** Connections → Data sources → verify VictoriaMetrics URL `http://victoriametrics:8428`.

## Adding new dashboards

1. Create the dashboard JSON.
2. Copy it to `/volume1/docker/cel/grafana-dashboards/`.
3. It auto-loads into the "CEL" folder within ~10s (no restart).

## More information

See **[PARSING_GUIDE.md](../PARSING_GUIDE.md)** for metric definitions, product
codes (ebIX, VSE), E66 vs E31 file types, and data quality (Condition 21).
