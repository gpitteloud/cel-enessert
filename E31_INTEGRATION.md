# E31 Community Aggregate Data Integration

## Summary

This document provides E31-specific Grafana queries and integration details. For complete technical background on E31 vs E66 files, product codes, and data structure, see **[PARSING_GUIDE.md](PARSING_GUIDE.md)**.

**Quick facts:**
- E31 files contain community-level aggregates (not individual meters)
- 6 E31 files delivered daily (3 consumption + 3 production)
- Flow characteristics: E17 (consumption), E18 (production)
- All E31 data has Condition 21 (estimated)

## What Changed

### New Parser: `parse_sdat_e31_aggregated.py`

**Purpose**: Parse E31 XML files containing community aggregates

**Features**:
- Handles both ebIX codes (8716867000030) and VSE codes (2404050010123, 2404050010124)
- Extracts community metadata (ID, type, grid area)
- Parses 480 observations per file (5 days × 96 intervals)
- Classifies each file into `direction` + `segment` (see `classify_metric_type`
  in `models.py`), which is what the dashboards filter on

**Table**: `cel_community_energy` (E66 goes to `cel_energy`; two tables so a
`sum()` cannot mix per-meter readings with the aggregate that contains them)

**Columns**:
- `ts`: interval start, the designated timestamp
- `community_id`: "101110-002726" (CEL community ID)
- `community_type`: "CT01"
- `product_code`: ebIX or VSE code
- `direction`: `consumption` (flow E17) or `production` (flow E18)
- `segment`: `total` (ebIX) / `cel` (VSE ...123) / `grid` (VSE ...124)
- `grid_area`: "12Y-0000000719-J"
- `value`: `DECIMAL(12,3)`, kWh for the 15-min interval
- `condition`: "21" — payload, **never** a dedup key, because the provider
  revises the grade across deliveries (see [QUESTDB.md](QUESTDB.md))

Dedup keys: `(ts, direction, segment, product_code, community_id)`.

### Updated: `watch_ftproot.py`

**Changes**:
- Imports E31 parser alongside E66 parser
- Detects file type by checking for `_E31_` or `_E66_` in filename
- Routes to appropriate parser based on file type
- Both file types are archived after successful processing

## E31 File Breakdown

**Daily delivery**: 6 E31 files (same delivery window as E66 files: 09:45-09:50)

### By Product Code:
- **2 files**: ebIX 8716867000030 (Total energy)
- **2 files**: VSE 2404050010123 (CEL Local)
- **2 files**: VSE 2404050010124 (Grid)

### By Flow Characteristic:
- **3 files**: E17 (Consumption) - Total, CEL Local, Grid
- **3 files**: E18 (Production) - Total, CEL Local, Grid

### File Pattern:
```
E17 (Consumption):
  - Total:      8716867000030_E17
  - CEL Local:  2404050010123_E17
  - Grid:       2404050010124_E17

E18 (Production):
  - Total:      8716867000030_E18
  - CEL Local:  2404050010123_E18
  - Grid:       2404050010124_E18
```

## Data Quality

All E31 observations have `<rsm:Condition>21</rsm:Condition>` (estimated/calculated data).

This is consistent with E66 VSE breakdown data - the provider uses estimation algorithms for community-level breakdowns.

## Grafana Usage

### Query Examples

Panels filter on `segment` rather than `product_code`: the same distinction,
under the name the parser derives, so a provider-side encoding change cannot
silently empty a panel. Every output column is `cast(... AS DOUBLE)` because the
Grafana plugin has no `DECIMAL` converter — cast the outer expression only, so
the inner `sum()` stays exact.

**Community total consumption** (energy per bucket, kWh):
```sql
SELECT ts AS time, cast(sum(value) AS DOUBLE) AS "Total Consumption"
FROM cel_community_energy
WHERE $__timeFilter(ts)
  AND community_id = '101110-002726'
  AND segment = 'total'
  AND direction = 'consumption'
SAMPLE BY $__sampleByInterval FILL(NULL)
ORDER BY time;
```

**Community CEL local vs grid consumption** — same query with
`segment = 'cel'` (consumed from within the community) or `segment = 'grid'`
(consumed from the external grid).

**Community production** — same again with `direction = 'production'`.

**As average power (kW)** instead of energy per bucket: average per 15-min slot
first, and only then scale by the integer `4`. `sum(value) * 4` is 4× wrong at a
1h bucket.
```sql
SELECT ts AS time, cast(avg(slot_kwh) * 4 AS DOUBLE) AS "From CEL"
FROM (
  SELECT ts, sum(value) AS slot_kwh
  FROM cel_community_energy
  WHERE $__timeFilter(ts)
    AND community_id = '101110-002726'
    AND segment = 'cel'
    AND direction = 'consumption'
  SAMPLE BY 15m
)
SAMPLE BY $__sampleByInterval FILL(NULL)
ORDER BY time;
```

### Dashboard Ideas

**Community Overview Dashboard**:
- Total community consumption vs production
- Self-sufficiency rate: CEL local / Total consumption
- Grid dependency: Grid consumption / Total consumption
- Compare aggregate vs sum of individual meters

**Validation Dashboard** — both are live in
`grafana-dashboards/grafana-dashboard-e31-v2.json`, panels 13-15:
```sql
-- Community aggregate
SELECT ts AS time, cast(sum(value) AS DOUBLE) AS "E31 Total"
FROM cel_community_energy
WHERE $__timeFilter(ts)
  AND community_id = '101110-002726'
  AND segment = 'total' AND direction = 'consumption'
SAMPLE BY $__sampleByInterval FILL(NULL) ORDER BY time;

-- vs the sum of the individual meters
SELECT ts AS time, cast(sum(value) AS DOUBLE) AS "Sum(E66)"
FROM cel_energy
WHERE $__timeFilter(ts)
  AND community_id = '101110-002726'      -- load-bearing, see below
  AND segment = 'total' AND direction = 'consumption'
SAMPLE BY $__sampleByInterval FILL(NULL) ORDER BY time;
```

**The `community_id` filter on the E66 side is not cosmetic.** The provider
delivers E66 files for 8 meters with no `<Community>` element, so their
`community_id` is NULL and they are absent from the E31 aggregate. Without the
filter the E66 side is overstated by ~24% (consumption) / ~33% (production),
which looks exactly like a validation failure and is not one. See
[QUESTDB.md](QUESTDB.md#known-data-anomalies) for the meter list and the other
known residuals.

## Deployment

### Files to Deploy:
1. `/app/scripts/parse_sdat_e31_aggregated.py` (new)
2. `/app/scripts/watch_ftproot.py` (updated)

### Steps:
```bash
# On development machine
scp cel-community/scripts/parse_sdat_e31_aggregated.py synology:/volume1/docker/cel/scripts/
scp cel-community/scripts/watch_ftproot.py synology:/volume1/docker/cel/scripts/

# On Synology
docker restart cel-parser

# Verify
docker logs -f cel-parser
```

### Testing:
```bash
# Test E31 parser standalone
docker exec cel-parser python3 /app/scripts/parse_sdat_e31_aggregated.py \
  /data/incoming/20260528_094741_12X-0000001536-1_E31_12X-00000020FW-5_813bf77c-5a69-11f1-b257-00000084413a.xml

# Check QuestDB for E31 data. Its ports are unpublished, so run from a
# container on cel-network.
docker exec cel-parser python3 -c \
  "import os, psycopg; print(psycopg.connect(os.environ['QUESTDB_DSN']).execute(
      'SELECT segment, direction, count() FROM cel_community_energy'
  ).fetchall())"

# Or check the day's balance end to end
docker exec -it cel-parser python3 \
  /app/scripts/validate_daily_balance_questdb.py 20260610
```

## Benefits

1. **Community-level visibility**: See total community consumption/production
2. **Validation**: Compare community aggregates vs sum of individual meters
3. **Self-sufficiency metrics**: Track CEL local vs grid energy
4. **Completeness**: Process all daily files (E66 individual meters + 6 E31 community aggregates)

## Notes

- E31 files have same 5-day overlapping pattern as E66 files
- `DEDUP UPSERT KEYS` makes the newest write win for a repeated slot (same
  behaviour as E66), so overlapping deliveries need no special handling — but
  replay must run in ascending delivery order
- All E31 data marked as estimated (Condition 21)
- No meter IDs in E31 - community-level only
- Same resolution: 15 minutes, 480 observations per file

## Related Documentation

- `FILE_BREAKDOWN_ANALYSIS.md` - Daily file delivery breakdown
- `QUESTDB.md` - Schema, dedup rules, Grafana plugin constraints, data anomalies
- `grafana-dashboards/README.md` - Dashboard queries and panel conventions
