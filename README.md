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

**Option 1: Automated deployment script**
```bash
./deploy_to_synology.sh admin@<synology-ip>
```

**Option 2: Manual deployment**
- Copy 5 Python scripts to `/volume1/docker/cel-parser/scripts/`
- Copy config file to `/volume1/docker/cel-parser/config/`
- See **[QUICK_START_SYNOLOGY.md](QUICK_START_SYNOLOGY.md)** for detailed steps

### 2. Configure System

Edit `/volume1/docker/cel-parser/config/api_config.yaml` if needed (default settings work):

```yaml
victoriametrics:
  url: "http://victoriametrics:8428"

project:
  name: "cel"
  community_name: "CEL Bern Quartier"
```

**Note:** Meter identification is automatic from SDAT XML. Household info (names, addresses) should be managed in a separate CRM system linked by meter_id.

### 3. Deploy Stack in Portainer

1. Open Portainer: http://192.168.1.133:9000
2. Stacks → Add stack → Name: `cel`
3. Copy content from `docker-compose.yml`
4. Deploy

### 4. Access Services

- **Grafana:** http://192.168.1.133:3000 (admin/admin)
- **VictoriaMetrics:** http://192.168.1.133:8428

### 5. Upload SDAT Files via FTP

Provider sends XML files to your Synology FTP, or you upload manually to `/volume1/ftproot/`.

Files are automatically:
1. Detected by cel-parser
2. Parsed and validated
3. Sent to VictoriaMetrics
4. Archived to `/volume1/docker/cel-parser/archive/`

## Architecture

```
Provider uploads SDAT XML via FTP
         ↓
/volume1/ftproot/
         ↓
cel-parser (watches for new files)
         ↓
VictoriaMetrics (stores metrics)
         ↓
Grafana (displays dashboards)
```

## Docker Containers

1. **victoriametrics** - Time series database (port 8428)
2. **grafana** - Visualization dashboards (port 3000)
3. **cel-parser** - Watches `/volume1/ftproot` and processes XML files automatically

## File Structure on Synology

```
/volume1/docker/cel-parser/
├── scripts/
│   ├── parse_sdat_v16.py              # E66 parser (individual meters)
│   ├── parse_e31_aggregated.py        # E31 parser (community aggregates)
│   ├── discover_meter_mappings.py     # Auto-discover physical-virtual mappings
│   ├── send_to_victoriametrics.py     # Sends data to VM
│   └── watch_ftproot.py               # Batch processor (auto-runs)
├── config/
│   ├── api_config.yaml                # VictoriaMetrics URL and project settings
│   └── meter_mappings.yaml            # Physical-virtual meter mappings (auto-generated)
├── logs/
│   └── watcher.log                    # Processing logs
├── archive/                            # Processed XML files
├── victoria-data/                      # VictoriaMetrics data
├── grafana-data/                       # Grafana data
├── grafana-provisioning/               # Grafana config
│   ├── datasources/
│   │   └── victoriametrics.yaml
│   └── dashboards/
│       └── dashboards.yaml
└── grafana-dashboards/                 # Dashboard JSON files
```

## Metrics Stored

### Per Meter (Individual)
```
cel_energy_grid_import_kwh{meter_id="CH756001234567890", project="cel"}
cel_energy_grid_export_kwh{meter_id="CH756001234567890", project="cel"}
cel_energy_local_import_kwh{meter_id="CH756001234567890", project="cel"}
cel_energy_local_export_kwh{meter_id="CH756001234567890", project="cel"}
cel_energy_consumed_kwh{meter_id="CH756001234567890", project="cel"}
cel_energy_produced_kwh{meter_id="CH756001234567890", project="cel"}
```

### Community Aggregates
```
cel_community_grid_import_kwh{project="cel"}
cel_community_grid_export_kwh{project="cel"}
cel_community_local_import_kwh{project="cel"}
cel_community_local_export_kwh{project="cel"}
cel_community_consumption_total_kwh{project="cel"}
cel_community_production_total_kwh{project="cel"}
```

## Available Dashboards

1. **Community Grid Overview**
   - Total community grid import/export
   - Net community position
   - Peak grid times
   - Top importers/exporters

2. **Meter Grid Usage**
   - Per-meter grid import/export over time
   - Local trading (import/export with community)
   - Production and consumption totals
   - Comparison with community average

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
sudo cat /volume1/docker/cel-parser/logs/watcher.log
```

**Test VictoriaMetrics:**
```bash
curl http://192.168.1.133:8428/health
curl 'http://192.168.1.133:8428/api/v1/query?query=cel_energy_grid_import_kwh'
```

## Troubleshooting

**Common issues:**

1. **Grafana not accessible**
   - Check: `sudo docker logs cel-grafana`
   - Fix permissions: `sudo chown -R 472:472 /volume1/docker/cel-parser/grafana-data`
   - Check firewall: DSM → Security → Firewall → Allow port 3000

2. **Files not being processed**
   - Check: `sudo docker logs cel-parser`
   - Verify files are in `/volume1/ftproot/`
   - Check permissions: `sudo chmod 755 /volume1/ftproot`

3. **Permission denied errors**
   - Run: `sudo chmod -R 755 /volume1/docker/cel-parser`
   - Grafana: `sudo chown -R 472:472 /volume1/docker/cel-parser/grafana-data`
   - VictoriaMetrics: `sudo chown -R 472:472 /volume1/docker/cel-parser/victoria-data`

## Configuration

### Edit System Config

```bash
ssh admin@192.168.1.133
sudo vi /volume1/docker/cel-parser/config/api_config.yaml
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
sudo docker restart cel-grafana cel-victoriametrics cel-parser
```

**Update configuration:**
```bash
# Edit config
sudo vi /volume1/docker/cel-parser/config/api_config.yaml
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
find /volume1/docker/cel-parser/archive -name "*.xml" -mtime +180 -delete
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
2. Verify config: `sudo cat /volume1/docker/cel-parser/config/api_config.yaml`
3. Test manually: `sudo docker exec -it cel-parser python /app/scripts/parse_sdat.py /data/incoming/test.xml --dry-run`

## Documentation

### Quick Navigation

**What do you want to do?**

| Task | Document |
|------|----------|
| 🎓 **Understand the system** | [PARSING_GUIDE.md](PARSING_GUIDE.md) - Complete technical reference |
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
3. **[QUICK_START_SYNOLOGY.md](QUICK_START_SYNOLOGY.md)** 🚀 - Deployment instructions

### Reference Documents

- **[FILE_BREAKDOWN_ANALYSIS.md](FILE_BREAKDOWN_ANALYSIS.md)** - Daily file delivery breakdown by member type
- **[E31_INTEGRATION.md](E31_INTEGRATION.md)** - E31 community aggregates and Grafana queries
- **[PROVIDER_QUESTIONS.md](PROVIDER_QUESTIONS.md)** - Questions for energy provider

### Operations

- **[DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)** - E31 integration deployment steps
- **[docker-compose.yml](docker-compose.yml)** - Docker stack configuration
- **[deploy_to_synology.sh](deploy_to_synology.sh)** - Automated deployment script

---

**Version:** 2.0 (E66 + E31 support, auto-discovery, batch processing)  
**Last updated:** 2026-06-30  
**Deployed on:** Synology NAS
