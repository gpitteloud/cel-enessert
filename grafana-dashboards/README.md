# CEL Grafana Dashboards

Production dashboards for CEL community energy monitoring.

## Available Dashboards

### 1. cel_energy_overview.json
**Individual meter dashboard** - Per-meter energy consumption and production

**Features:**
- Meter selector dropdown (auto-populated)
- Consumption and production charts with CEL/Grid breakdown
- Total stats panels
- Energy balance (net production - consumption)
- Stacked area charts

**Metrics used:**
- `cel_energy_consumed_kwh`
- `cel_energy_produced_kwh`
- `cel_energy_grid_import_kwh`
- `cel_energy_grid_export_kwh`
- `cel_energy_local_import_kwh`
- `cel_energy_local_export_kwh`

### 2. grafana-dashboard-e31-community.json
**Community aggregate dashboard** - Community-level totals and statistics

**Features:**
- Total community consumption and production
- CEL Local vs Grid breakdown
- Self-sufficiency percentage
- Grid dependency
- Validation: E31 vs sum of E66 meters

**Metrics used:**
- `energy_community_aggregate_kwh` (E31 data)
- Flow characteristics: E17 (consumption), E18 (production)
- Product codes: ebIX 8716867000030, VSE 2404050010123, VSE 2404050010124

## Installation

### Automatic (Provisioning)

Dashboards in this directory are automatically loaded by Grafana on startup via docker-compose mount:

```yaml
volumes:
  - /volume1/docker/cel-parser/grafana-dashboards:/var/lib/grafana/dashboards
```

### Manual Import

1. Open Grafana: http://<synology-ip>:3000
2. Login: admin/admin
3. Dashboards → Import
4. Upload JSON file
5. Select datasource: VictoriaMetrics
6. Click Import

## Queries

All queries use VictoriaMetrics datasource at `http://victoriametrics:8428`

**Example queries:**
```promql
# Individual meter consumption
cel_energy_consumed_kwh{meter_id=~".*${meter_id}"}

# Community aggregate consumption
energy_community_aggregate_kwh{community_id="101110-002726",flow_characteristic="E17"}
```

## Troubleshooting

**No data displayed:**
1. Check VictoriaMetrics: `curl http://localhost:8428/health`
2. Verify data exists: `curl 'http://localhost:8428/api/v1/series?match[]=cel_energy_consumed_kwh'`
3. Check cel-parser logs: `docker logs cel-parser`

**Meter selector empty:**
- No data in VictoriaMetrics yet
- Process files to populate data

**Wrong datasource:**
- Go to Connections → Data sources
- Verify VictoriaMetrics URL: `http://victoriametrics:8428`

## Adding New Dashboards

1. Create dashboard JSON file
2. Copy to this directory: `/volume1/docker/cel-parser/grafana-dashboards/`
3. Restart Grafana: `docker restart grafana`
4. Dashboard auto-loads in "CEL" folder

## Dashboard Organization

Dashboards are auto-organized into "CEL" folder via provisioning config:
- `grafana-provisioning/dashboards/dashboards.yaml`

## More Information

See **[PARSING_GUIDE.md](../PARSING_GUIDE.md)** for:
- Metric definitions
- Product codes (ebIX, VSE)
- E66 vs E31 file types
- Data quality (Condition 21)
