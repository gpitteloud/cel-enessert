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
- Transforms to VictoriaMetrics format with community-level labels

**Metric name**: `energy_community_aggregate_kwh`

**Labels**:
- `community_id`: "101110-002726" (CEL community ID)
- `community_type`: "CT01"
- `product_code`: ebIX or VSE code
- `flow_characteristic`: E17 (consumption) or E18 (production)
- `grid_area`: "12Y-0000000719-J"
- `data_source`: "E31_AggregatedMeteredData"
- `condition`: "21" (all E31 data is estimated)

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

**Community total consumption**:
```promql
energy_community_aggregate_kwh{
  community_id="101110-002726",
  product_code="8716867000030",
  flow_characteristic="E17"
}
```

**Community CEL local consumption** (energy consumed from within community):
```promql
energy_community_aggregate_kwh{
  community_id="101110-002726",
  product_code="2404050010123",
  flow_characteristic="E17"
}
```

**Community grid consumption** (energy consumed from external grid):
```promql
energy_community_aggregate_kwh{
  community_id="101110-002726",
  product_code="2404050010124",
  flow_characteristic="E17"
}
```

**Community total production**:
```promql
energy_community_aggregate_kwh{
  community_id="101110-002726",
  product_code="8716867000030",
  flow_characteristic="E18"
}
```

**Community CEL local production** (energy produced and consumed within community):
```promql
energy_community_aggregate_kwh{
  community_id="101110-002726",
  product_code="2404050010123",
  flow_characteristic="E18"
}
```

### Dashboard Ideas

**Community Overview Dashboard**:
- Total community consumption vs production
- Self-sufficiency rate: CEL local / Total consumption
- Grid dependency: Grid consumption / Total consumption
- Compare aggregate vs sum of individual meters

**Validation Dashboard**:
```promql
# Compare community aggregate vs sum of individual meters
# Should be approximately equal (differences due to estimation)

# Community aggregate
energy_community_aggregate_kwh{
  product_code="8716867000030",
  flow_characteristic="E17"
}

# vs

# Sum of individual meters
sum(energy_kwh{
  product_code="8716867000030",
  data_type="consumption"
})
```

## Deployment

### Files to Deploy:
1. `/app/scripts/parse_sdat_e31_aggregated.py` (new)
2. `/app/scripts/watch_ftproot.py` (updated)

### Steps:
```bash
# On development machine
scp cel-community/scripts/parse_sdat_e31_aggregated.py synology:/volume1/docker/cel-parser/scripts/
scp cel-community/scripts/watch_ftproot.py synology:/volume1/docker/cel-parser/scripts/

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

# Check VictoriaMetrics for E31 data
curl 'http://victoriametrics:8428/api/v1/series?match[]=energy_community_aggregate_kwh'
```

## Benefits

1. **Community-level visibility**: See total community consumption/production
2. **Validation**: Compare community aggregates vs sum of individual meters
3. **Self-sufficiency metrics**: Track CEL local vs grid energy
4. **Completeness**: Process all daily files (E66 individual meters + 6 E31 community aggregates)

## Notes

- E31 files have same 5-day overlapping pattern as E66 files
- VictoriaMetrics will overwrite duplicate timestamps (same behavior as E66)
- All E31 data marked as estimated (Condition 21)
- No meter IDs in E31 - community-level only
- Same resolution: 15 minutes, 480 observations per file

## Related Documentation

- `FILE_BREAKDOWN_ANALYSIS.md` - Daily file delivery breakdown
- `PARSER_MULTI_MEMBER_UPDATE.md` - E66 multi-member support
- Memory: `reference_e66_e31_file_types.md` - E66 vs E31 differences
