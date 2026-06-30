# Deployment Checklist - E31 Integration

**Date**: 2026-06-26  
**Purpose**: Deploy E31 community aggregate support + updated watcher

---

## Files to Deploy (2 files to Synology)

### 1. **NEW: E31 Parser**
```
Source: cel-community/scripts/parse_e31_aggregated.py
Target: synology:/volume1/docker/cel-parser/scripts/parse_e31_aggregated.py
```

**What it does**: Parses E31 AggregatedMeteredData_1.3 files (community-level data)

### 2. **UPDATED: File Watcher**
```
Source: cel-community/scripts/watch_ftproot.py
Target: synology:/volume1/docker/cel-parser/scripts/watch_ftproot.py
```

**What changed**: Now detects and processes both E66 and E31 files

---

## Deployment Commands

### Step 1: Copy Files to Synology
```bash
# From your development machine, in /home/copadev/projects/cel/

# Copy E31 parser (NEW)
scp cel-community/scripts/parse_e31_aggregated.py \
    synology:/volume1/docker/cel-parser/scripts/

# Copy updated watcher (UPDATED)
scp cel-community/scripts/watch_ftproot.py \
    synology:/volume1/docker/cel-parser/scripts/
```

### Step 2: Restart Parser Container
```bash
# SSH to Synology
ssh synology

# Restart the parser container
docker restart cel-parser

# Watch logs to verify it's working
docker logs -f cel-parser
```

### Step 3: Verify E31 Processing
```bash
# After a few minutes, check for E31 data in VictoriaMetrics
curl 'http://victoriametrics:8428/api/v1/series?match[]=energy_community_aggregate_kwh' | jq

# Should return series with labels:
# - community_id="101110-002726"
# - flow_characteristic="E17" or "E18"
# - product_code="8716867000030", "2404050010123", or "2404050010124"
```

---

## Grafana Dashboard (Optional - Import Manually)

### File: `grafana-dashboard-e31-community.json`

**Location**: `/home/copadev/projects/cel/grafana-dashboard-e31-community.json`

**Import Steps**:
1. Open Grafana web UI
2. Go to **Dashboards → Import**
3. Click **"Upload JSON file"**
4. Select `grafana-dashboard-e31-community.json`
5. Choose your **VictoriaMetrics datasource**
6. Click **Import**

**Dashboard Features**:
- Community consumption/production totals
- Self-sufficiency rate gauge
- Grid dependency gauge
- Consumption/production breakdown charts
- E31 vs E66 validation charts

---

## What Changes After Deployment

### Before (Current State):
- ❌ E31 files (6/day) are **ignored** - only E66 files (103/day) processed
- ❌ No community-level aggregate data in VictoriaMetrics
- ❌ Can't see community totals in Grafana

### After (New State):
- ✅ E31 files (6/day) are **processed** - all 109 files/day handled
- ✅ Community aggregate data available in VictoriaMetrics
- ✅ New metric: `energy_community_aggregate_kwh`
- ✅ Can view community totals, self-sufficiency, grid dependency in Grafana
- ✅ Can validate E31 aggregates vs sum of E66 meters

---

## Expected Logs After Deployment

```
2026-06-26 14:30:01 CEST - __main__ - INFO - Processing 20260626_094741_12X-0000001536-1_E31_12X-00000020FW-5_813bf77c.xml
2026-06-26 14:30:01 CEST - __main__ - INFO - 20260626_094741_12X-0000001536-1_E31_12X-00000020FW-5_813bf77c.xml: Parsed 480 community aggregate observations
2026-06-26 14:30:02 CEST - __main__ - INFO - Successfully processed 20260626_094741_12X-0000001536-1_E31_12X-00000020FW-5_813bf77c.xml (480 data points)
2026-06-26 14:30:02 CEST - __main__ - INFO - Archived 20260626_094741_12X-0000001536-1_E31_12X-00000020FW-5_813bf77c.xml to /data/archive/...
```

Look for:
- `Processing` messages for both `E31` and `E66` files
- `Parsed X community aggregate observations` for E31 files
- `Successfully processed` with data point counts
- No errors or warnings

---

## Rollback (If Needed)

If something goes wrong:

```bash
# SSH to Synology
ssh synology

# Stop the parser
docker stop cel-parser

# Restore old watcher (if you backed it up)
cp /volume1/docker/cel-parser/scripts/watch_ftproot.py.backup \
   /volume1/docker/cel-parser/scripts/watch_ftproot.py

# Remove E31 parser
rm /volume1/docker/cel-parser/scripts/parse_e31_aggregated.py

# Restart
docker start cel-parser
```

**Note**: E31 parser is standalone - removing it won't affect E66 processing.

---

## Verification Checklist

After deployment, verify:

- [ ] Parser container restarted successfully
- [ ] Logs show both E66 and E31 files being processed
- [ ] No errors in logs
- [ ] VictoriaMetrics has `energy_community_aggregate_kwh` metric
- [ ] Metric has correct labels (community_id, flow_characteristic, product_code)
- [ ] Grafana dashboard imported (if applicable)
- [ ] Dashboard shows data (may take 10-15 minutes for first data)
- [ ] All 109 files/day are processed (103 E66 + 6 E31)

---

## Troubleshooting

### Problem: E31 files not processing
**Check**: 
```bash
docker exec cel-parser ls -la /app/scripts/parse_e31_aggregated.py
```
Should show the file exists

### Problem: Import errors in logs
**Check**: 
```bash
docker exec cel-parser python3 -c "from parse_e31_aggregated import parse_e31_xml; print('OK')"
```
Should print "OK"

### Problem: No E31 data in VictoriaMetrics
**Check**:
1. Are E31 files in `/data/incoming`?
2. Are they being archived to `/data/archive`?
3. Check VictoriaMetrics URL in config: `/app/config/config.yaml`

### Problem: E66 files stopped working
**Check**: 
```bash
# Verify both parsers are importable
docker exec cel-parser python3 -c "from parse_sdat_v16 import parse_sdat_xml; print('E66 OK')"
docker exec cel-parser python3 -c "from parse_e31_aggregated import parse_e31_xml; print('E31 OK')"
```

---

## Summary

**Deploy**: 2 files  
**Restart**: 1 container  
**Import**: 1 Grafana dashboard (optional)  
**Result**: Process all 109 files/day (103 E66 + 6 E31)  
**New metric**: `energy_community_aggregate_kwh`

**Time estimate**: 5 minutes (excluding first data arrival wait)
