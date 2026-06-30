# Quick Start - Synology Deployment

Ultra-fast deployment guide for Synology NAS + Portainer.

## Prerequisites

- ✅ Synology NAS with Docker installed
- ✅ Portainer running (usually on port 9000)
- ✅ SSH enabled on Synology
- ✅ FTP enabled on Synology

## 1. Deploy Files (5 minutes)

Run the deployment script from your computer:

```bash
cd cel-community
./deploy_to_synology.sh admin@192.168.1.133
```

Replace `192.168.1.133` with your NAS IP.

This copies all files to `/volume1/docker/cel/` on your Synology.

## 2. Verify Configuration (1 minute)

SSH into Synology:

```bash
ssh admin@192.168.1.133
sudo cat /volume1/docker/cel/config/api_config.yaml
```

Default configuration should work. Meters are identified automatically from SDAT XML.

**Note:** Household info (names, addresses) should be managed in a separate CRM system linked by meter_id.

## 3. Deploy Stack in Portainer (3 minutes)

1. Open Portainer: `http://192.168.1.133:9000`
2. Go to: **Stacks** → **Add stack**
3. Name: `cel` (must match /volume1/docker/cel directory)
4. Copy this stack configuration:

```yaml
version: '3.8'

services:
  victoriametrics:
    image: victoriametrics/victoria-metrics:latest
    container_name: cel-victoriametrics
    restart: unless-stopped
    ports:
      - "8428:8428"
    volumes:
      - /volume1/docker/cel/victoria-data:/victoria-metrics-data
    command:
      - "--storageDataPath=/victoria-metrics-data"
      - "--httpListenAddr=:8428"
      - "--retentionPeriod=24"
    networks:
      - cel-network

  grafana:
    image: grafana/grafana:latest
    container_name: cel-grafana
    restart: unless-stopped
    ports:
      - "3000:3000"
    volumes:
      - /volume1/docker/cel/grafana-data:/var/lib/grafana
      - /volume1/docker/cel/grafana-provisioning:/etc/grafana/provisioning
      - /volume1/docker/cel/grafana-dashboards:/var/lib/grafana/dashboards
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=admin
    depends_on:
      - victoriametrics
    networks:
      - cel-network

  cel-parser:
    image: python:3.11-slim
    container_name: cel-parser
    restart: unless-stopped
    volumes:
      - /volume1/ftproot:/data/incoming:ro
      - /volume1/docker/cel/scripts:/app/scripts
      - /volume1/docker/cel/config:/app/config
      - /volume1/docker/cel/logs:/app/logs
      - /volume1/docker/cel/archive:/data/archive
    working_dir: /app
    command: >
      sh -c "
        pip install --no-cache-dir requests pyyaml watchdog &&
        python scripts/watch_ftproot.py
      "
    environment:
      - VICTORIA_METRICS_URL=http://victoriametrics:8428
    depends_on:
      - victoriametrics
    networks:
      - cel-network

networks:
  cel-network:
    driver: bridge
```

5. Click **Deploy the stack**
6. Wait 2-3 minutes for containers to start

## 4. Verify (2 minutes)

Check containers in Portainer → **Containers**:

- ✅ `cel-victoriametrics` - running
- ✅ `cel-grafana` - running  
- ✅ `cel-parser` - running

Open Grafana: `http://192.168.1.133:3000`
- Login: `admin` / `admin`
- Change password

## 5. Test with FTP (5 minutes)

Create test file `test.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<SDAT xmlns="http://www.strom.ch">
  <Messung>
    <Zaehler>CH1234567890</Zaehler>
    <Zeitstempel>2026-05-11T12:00:00</Zeitstempel>
    <Wert>
      <OBIS>1.8.0</OBIS>
      <Wert>0.5</Wert>
    </Wert>
    <Wert>
      <OBIS>2.8.0</OBIS>
      <Wert>1.2</Wert>
    </Wert>
  </Messung>
</SDAT>
```

Upload via FTP:

```bash
ftp 192.168.1.133
# Login with Synology credentials
put test.xml
quit
```

Check processing in Portainer:
- **Containers** → `cel-parser` → **Logs**
- Should see: "Successfully processed test.xml"

Check data in Grafana:
- **Explore** → Query: `cel_energy_grid_import_kwh`
- Should show test data

## Done! 🎉

Your CEL system is now running and monitoring `/volume1/ftproot` for new SDAT files.

## Configure Provider

Give your electricity provider these FTP details:

```
Host: 192.168.1.133 (or your-nas.synology.me)
Port: 21
Username: [your FTP user]
Password: [your FTP password]
Directory: /
File format: SDAT XML
```

Files uploaded via FTP will be automatically:
1. Detected by cel-parser
2. Parsed and validated
3. Sent to VictoriaMetrics
4. Archived to `/volume1/docker/cel/archive/`

## Troubleshooting

### Parser not processing files

Check logs:
```bash
# Via SSH
tail -f /volume1/docker/cel/logs/watcher.log

# Via Portainer
Containers → cel-parser → Logs
```

### No data in Grafana

Test VictoriaMetrics:
```bash
ssh admin@192.168.1.133
curl http://localhost:8428/api/v1/query?query=cel_energy_grid_import_kwh
```

### FTP not working

Synology: **Control Panel** → **File Services** → **FTP**
- Enable FTP service
- Check port (default: 21)
- Allow FTP user access

## Next Steps

- Set up Grafana dashboards for meter monitoring
- Configure provider FTP access
- Set up monitoring/alerting
- Build separate CRM system to manage household info (names, addresses) linked by meter_id

## Support

- Full setup guide: `SYNOLOGY_SETUP.md`
- System limitations: `LIMITATIONS.md`
- Architecture details: `README_SIMPLE.md`
