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
- `segment` = `cel` \| `grid` \| `total`  (total = cel + grid; total is *measured*, cel/grid are *estimated*, Condition 21)
- `product_code` (`8716867000030` = total, `2404050010123` = CEL, `2404050010124` = grid), `code_type`, `community_id`
- E66 also: `meter_id`; E31 also: `community_type`, `grid_area`

> Two distinct metric names are kept deliberately so `sum()` over per-meter data
> is never confused with the community aggregate.

## Power vs energy on line charts

Stored samples are **energy per 15-min interval** (kWh/15min), which is additive.
If plotted raw, Grafana's downsampling picks one sample per step instead of
summing, so a zoomed-out chart under-reads by up to ~4×.

The timeseries (line) panels therefore display **average power in kW** instead:

```promql
(avg_over_time(sum without(condition)(<selector>)[$__interval:15m])) * 4
```

- `avg_over_time(...[$__interval:15m])` averages every 15-min sample inside the
  step bucket, so energy is conserved at any zoom level.
- `* 4` converts kWh per 15-min (0.25 h) to kW.
- The `:15m` subquery step floors the resolution at the native 15 min so
  `$__interval` never drops below it (no empty buckets / gaps).
- `sum without(condition)(...)` — see next section.
- Unit is `kwatt` (kW).

Stat and pie panels don't need the power conversion (their panel-level reduce
`sum` totals energy over the range correctly), but they **do** need the
`condition` collapse below — so their bare selectors are wrapped in
`sum without(condition)(...)`. Gauge panels already use `sum(increase(...))`,
which collapses `condition` on its own, so they were left as-is.

## The `condition` label — collapse it in every query

The provider marks each 15-min reading as **measured** (no `condition`) or
**estimated** (`condition="21"`), and mixes both within a single series over
time. Because `condition` is a stored VM label, every `(direction, segment)`
therefore resolves to **two time series** — one `{condition="21"}` and one with
no `condition`. Left alone this makes each line/slice/stat appear **twice** (the
symptom that motivated this).

**Rule: no panel should ever expose a raw `cel_energy_kwh` /
`cel_community_energy_kwh` selector.** Always collapse `condition` first:

- Line panels: `avg_over_time(sum without(condition)(<selector>)[$__interval:15m]) * 4`
- Stat / pie panels (bare selectors): `sum without(condition)(<selector>)`
- Gauge panels: `sum(increase(<selector>[$__range]))` already collapses it.

Note the aggregation order for line panels: the `sum` goes **inside** the
`avg_over_time` subquery. The two condition variants share the hour bucket at the
measured→estimated transition, so averaging *before* collapsing
(`sum without(condition)(avg_over_time(...))`) would double-count that bucket.

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

- **Production:** the difference panel should sit at **≈ 0** (E31 total ==
  sum of E66 physical production totals, exact to rounding, e.g. both 2558.6 kWh
  on 2026-06-15). A large gap here means duplicate virtual-meter totals have
  crept back in (see next section).
- **Consumption:** currently **not** ≈ 0, for a provider-side reason, not a bug:
  E31 consumption is delivered as all-zero from data date **2026-06-01 onward**,
  and a monthly E31 file injects consumption from 2026-04-30 where no E66 exists.
  Until the provider clarifies (see `PROVIDER_QUESTIONS.md` Q16a/Q16b), treat the
  E66 sum as the source of truth for community consumption.

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
