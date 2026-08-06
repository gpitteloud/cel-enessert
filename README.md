# CEL Energy Monitoring - Synology Deployment

Monitors Swiss local energy communities (CEL) using provider XML files (ValidatedMeteredData_1.6 and AggregatedMeteredData_1.3).

## Overview

**Daily delivery**: Multiple XML files (E66 individual meters + 6 E31 community aggregates)
- File count varies based on number of members and whether they have production
- Example: 21 members (9 with solar) = 109 files daily (103 E66 + 6 E31)

**What this system tracks:**
- ✅ Individual meter consumption and production (total and breakdown)
- ✅ CEL Local vs Grid energy exchange per meter
- ✅ Community-level aggregate statistics
- ✅ 15-minute resolution data (96 intervals/day)
- ✅ Physical and virtual meter data attribution

**For complete technical details**, see **[PARSING_GUIDE.md](PARSING_GUIDE.md)** - explains file types, meter types, product codes, data quality flags, and more.

## Quick Start

### 1. Deploy Files to Synology

Copy the scripts to `/volume1/docker/cel/scripts/`, the config to
`/volume1/docker/cel/config/`, and the dashboards to
`/volume1/docker/cel/grafana-dashboards/`. See
**[QUICK_START_SYNOLOGY.md](QUICK_START_SYNOLOGY.md)** for the detailed steps.

### 2. Configure System

`config/api_config.yaml` works as shipped — the QuestDB DSN comes from
`$QUESTDB_DSN`, which `docker-compose.yml` sets:

```yaml
questdb:
  dsn: ""            # falls back to $QUESTDB_DSN

project:
  name: "cel"
  community_name: "Coopérative Enessert"
```

**Note:** Meter identification is automatic from SDAT XML. Household info (names, addresses) should be managed in a separate CRM system linked by meter_id.

### 3. Deploy Stack in Portainer

1. Create the `cel-network` bridge network once (Portainer → Networks → Add, or
   `docker network create cel-network`) — both stacks attach to it and neither
   owns it.
2. Open Portainer: http://192.168.1.133:9000
3. Stacks → Add stack → Name: `cel`
4. Copy content from `docker-compose.yml`
5. Deploy

Deploy over SSH or the LAN IP, never through the Cloudflare tunnel: the
`questdb-init` container pip-installs and verifies the schema before the parser
is allowed to start, which can take 1-2 minutes on a cold start and exceeds the
tunnel's ~100s origin timeout.

### 4. Access Services

- **Grafana:** http://192.168.1.133:3000

QuestDB's console (9000) and PG-wire port (8812) are **deliberately not
published** — see [QUESTDB.md](QUESTDB.md#security).

### 5. Upload SDAT Files via FTP

Provider sends XML files to your Synology FTP, or you upload manually to `/volume1/ftproot/`.

Files are automatically:
1. Detected by cel-parser
2. Parsed and validated
3. Written to QuestDB
4. Archived to `/volume1/docker/cel/archive/`

A file whose write fails is **not** archived: it stays in the incoming folder and
is retried, so nothing is ever filed away having stored no data.

## Architecture

```
Provider uploads SDAT XML via FTP
         ↓
/volume1/ftproot/
         ↓
cel-parser (watches for new files)
         ↓
QuestDB (stores measurements, last write wins)
         ↓
Grafana (displays dashboards)
```

## Docker Containers

1. **questdb** - Time-series database, the system of record (ports unpublished)
2. **questdb-init** - One-shot schema apply + verify; the parser waits for it
3. **grafana** - Visualization dashboards (port 3000)
4. **cel-parser** - Watches `/volume1/ftproot` and processes XML files automatically

## File Structure on Synology

```
/volume1/docker/cel/
├── scripts/
│   ├── parse_sdat.py                  # Dispatcher (E66 vs E31)
│   ├── parse_sdat_e66_individual.py   # E66 parser (individual meters)
│   ├── parse_sdat_e31_aggregated.py   # E31 parser (community aggregates)
│   ├── models.py                      # MeteredData, MetricType, classification
│   ├── discover_meter_mappings.py     # Auto-discover physical-virtual mappings
│   ├── questdb_schema.sql             # Authoritative DDL
│   ├── questdb_init.py                # Applies + verifies the schema
│   ├── questdb_writer.py              # Writes rows over PG-wire
│   └── watch_ftproot.py               # Batch processor (auto-runs)
├── config/
│   ├── api_config.yaml                # DSN and project settings
│   └── meter_mappings.yaml            # Physical-virtual meter mappings (auto-generated)
├── logs/
│   └── watcher.log                    # Processing logs
├── archive/                            # Processed XML files
├── questdb-data/                       # QuestDB data
├── grafana-data/                       # Grafana data
├── grafana-provisioning/               # Grafana config
│   ├── datasources/
│   │   └── questdb.yaml
│   └── dashboards/
│       └── dashboards.yaml
└── grafana-dashboards/                 # Dashboard JSON files
```

## What Is Stored

Two tables, so that a `sum()` cannot mix per-meter readings with the community
aggregate that already contains them. Full schema and design rationale in
**[QUESTDB.md](QUESTDB.md)**.

```sql
cel_energy           -- E66: one row per (ts, meter_id, direction, segment,
                     --      product_code, community_id)
cel_community_energy -- E31: one row per (ts, direction, segment,
                     --      product_code, community_id)
cel_ingest_log       -- provenance: ~1 row per processed file
```

`direction` is `consumption` | `production`; `segment` is `cel` | `grid` |
`total`; `value` is kWh for one 15-min slot, stored as `DECIMAL(12,3)` so the
provider's exact figure survives.

The provider re-sends each slot 5-7 times across overlapping deliveries and
revises ~2.6% of them, so the tables use `DEDUP UPSERT KEYS`: the newest write
wins. **This makes replay order load-bearing** — always replay archived
deliveries in ascending date order.

## Available Dashboards

1. **CEL Energy Overview** (`cel_energy_overview.json`) — the home dashboard
   - Per-meter consumption and production over time, with a meter selector
   - CEL-local vs grid split
   - Community totals and self-consumption percentages

2. **CEL Community Aggregates E31 v2** (`grafana-dashboard-e31-v2.json`)
   - Community consumption/production from the E31 aggregate
   - Self-sufficiency and grid dependency
   - Validation panels comparing E31 against the sum of every E66 meter

See **[grafana-dashboards/README.md](grafana-dashboards/README.md)** for the
queries and the plugin quirks they work around.

## What Provider Data Includes

The SDAT-CH2025 v2 XML file from your electricity provider contains:

- **Grid Import (OBIS 1.8.0)** - Energy bought from grid
- **Grid Export (OBIS 2.8.0)** - Energy sold to grid
- **Timestamps** - 15-minute intervals
- **Meter ID** - Smart meter identifier

**Why no solar production data?**

The smart meter at your grid connection only measures energy crossing it. Solar energy produced and immediately consumed never crosses the meter, so it's invisible to the provider.

```
Solar → Inverter → House (not measured)
            ↓
          Meter ← → Grid (measured)
```

## Monitoring

**Check container status:**
```bash
ssh admin@192.168.1.133
sudo docker ps | grep cel-
```

**View processing logs:**
```bash
# Real-time logs
sudo docker logs -f cel-parser

# Or check log file
sudo cat /volume1/docker/cel/logs/watcher.log
```

**Query QuestDB.** Its ports are not published, so run this from inside a
container on `cel-network`:
```bash
sudo docker exec -it cel-parser python3 \
    /app/scripts/validate_daily_balance_questdb.py 20260610

# Or ad-hoc SQL over the REST API from within the network:
sudo docker exec cel-parser sh -c \
    "python3 -c \"import urllib.request,urllib.parse;print(urllib.request.urlopen('http://questdb:9000/exec?'+urllib.parse.urlencode({'query':'SELECT count() FROM cel_energy'})).read().decode())\""
```

## Troubleshooting

**Common issues:**

1. **Grafana not accessible**
   - Check: `sudo docker logs cel-grafana`
   - Fix permissions: `sudo chown -R 472:472 /volume1/docker/cel/grafana-data`
   - Check firewall: DSM → Security → Firewall → Allow port 3000

2. **Files not being processed**
   - Check: `sudo docker logs cel-parser`
   - Verify files are in `/volume1/ftproot/`
   - Check permissions: `sudo chmod 755 /volume1/ftproot`

3. **Permission denied errors**
   - Run: `sudo chmod -R 755 /volume1/docker/cel`
   - Grafana: `sudo chown -R 472:472 /volume1/docker/cel/grafana-data`

4. **Parser will not start**
   - It waits for `cel-questdb-init` to exit 0, so check that first:
     `sudo docker logs cel-questdb-init`
   - A non-zero exit means the live schema does not match
     `scripts/questdb_schema.sql`. The parser deliberately refuses to start
     rather than write into a table without the dedup keys.

## Configuration

### Edit System Config

```bash
ssh admin@192.168.1.133
sudo vi /volume1/docker/cel/config/api_config.yaml
sudo docker restart cel-parser  # Reload config
```

**Note:** No household configuration needed. Meters are identified automatically from SDAT XML.

### Provider FTP Settings

Give your electricity provider:
```
Host: 192.168.1.133 (or your-domain.synology.me)
Port: 21
Protocol: FTP or FTPS
Directory: / (files go to /volume1/ftproot)
Format: SDAT-CH2025 v2 XML
```

## Maintenance

**Restart containers:**
```bash
sudo docker restart cel-grafana cel-questdb cel-parser
```

**Update configuration:**
```bash
# Edit config
sudo vi /volume1/docker/cel/config/api_config.yaml
# Restart to reload
sudo docker restart cel-parser
```

**Backup data:**
```bash
sudo tar czf ~/cel-backup-$(date +%Y%m%d).tar.gz /volume1/docker/cel
```

**Clean old archives:**
```bash
# Delete XML files older than 6 months
find /volume1/docker/cel/archive -name "*.xml" -mtime +180 -delete
```


## System Requirements

- Synology NAS with Docker support
- ~500 MB disk space for Docker images
- ~7 GB for 24 months of data (10 meters)
- Portainer installed (recommended)
- FTP access enabled

## Support

**Issues:**
1. Check logs: `sudo docker logs cel-parser`
2. Verify config: `sudo cat /volume1/docker/cel/config/api_config.yaml`
3. Test manually: `sudo docker exec -it cel-parser python /app/scripts/parse_sdat.py /data/incoming/test.xml --dry-run`

## Documentation

### Quick Navigation

**What do you want to do?**

| Task | Document |
|------|----------|
| 🎓 **Understand the system** | [PARSING_GUIDE.md](PARSING_GUIDE.md) - Complete technical reference |
| 🗄️ **Understand the database** | [QUESTDB.md](QUESTDB.md) - Schema, dedup rules, Grafana plugin traps |
| 🚀 **Deploy to Synology** | [QUICK_START_SYNOLOGY.md](QUICK_START_SYNOLOGY.md) - Deployment guide |
| 📊 **Understand daily files** | [FILE_BREAKDOWN_ANALYSIS.md](FILE_BREAKDOWN_ANALYSIS.md) - Daily file breakdown |
| 📈 **Query community data** | [E31_INTEGRATION.md](E31_INTEGRATION.md) - E31 Grafana queries |
| ❓ **Talk to provider** | [PROVIDER_QUESTIONS.md](PROVIDER_QUESTIONS.md) - Questions to validate |
| ✅ **Deploy updates** | [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md) - Deployment steps |

### Core Documentation

1. **[README.md](README.md)** ⭐ - This file (overview and quick start)
2. **[PARSING_GUIDE.md](PARSING_GUIDE.md)** 📘 - **Complete technical reference** (AUTHORITATIVE)
   - File types (E66 vs E31)
   - Meter types (physical vs virtual)
   - Product codes (ebIX, VSE)
   - Data quality (Condition 21)
   - Meter mapping discovery
3. **[QUESTDB.md](QUESTDB.md)** 🗄️ - **Storage reference** (AUTHORITATIVE for the schema)
   - Why the dedup keys are what they are
   - Why replay order is a correctness requirement
   - `DECIMAL(12,3)` and its consequences in queries
   - Grafana plugin constraints that fail silently
   - Known provider-side data anomalies
4. **[QUICK_START_SYNOLOGY.md](QUICK_START_SYNOLOGY.md)** 🚀 - Deployment instructions

### Reference Documents

- **[FILE_BREAKDOWN_ANALYSIS.md](FILE_BREAKDOWN_ANALYSIS.md)** - Daily file delivery breakdown by member type
- **[E31_INTEGRATION.md](E31_INTEGRATION.md)** - E31 community aggregates and Grafana queries
- **[PROVIDER_QUESTIONS.md](PROVIDER_QUESTIONS.md)** - Questions for energy provider

### Operations

- **[DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)** - E31 integration deployment steps
- **[docker-compose.yml](docker-compose.yml)** - Docker stack configuration
- **[docker-compose.cloudflared.yml](docker-compose.cloudflared.yml)** - Tunnel stack (publishes Grafana only)

---

**Version:** 3.0 (E66 + E31 support, auto-discovery, batch processing, QuestDB storage)  
**Last updated:** 2026-08-06  
**Deployed on:** Synology NAS
