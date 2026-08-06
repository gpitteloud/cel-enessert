# Quick Start - Synology Deployment

Ultra-fast deployment guide for Synology NAS + Portainer.

## Prerequisites

- ✅ Synology NAS with Docker installed
- ✅ Portainer running (usually on port 9000)
- ✅ SSH enabled on Synology
- ✅ FTP enabled on Synology

## 1. Deploy Files (5 minutes)

Copy the repo's runtime files to `/volume1/docker/cel/` on the NAS. Replace
`192.168.1.133` with your NAS IP.

```bash
cd cel-community
NAS=admin@192.168.1.133

ssh $NAS 'sudo mkdir -p /volume1/docker/cel/{scripts,config,logs,archive,questdb-data,grafana-data,grafana-dashboards,grafana-provisioning}'

scp scripts/*.py scripts/questdb_schema.sql   $NAS:/volume1/docker/cel/scripts/
scp config/api_config.yaml                    $NAS:/volume1/docker/cel/config/
scp grafana-dashboards/*.json                 $NAS:/volume1/docker/cel/grafana-dashboards/
scp -r grafana-provisioning/*                 $NAS:/volume1/docker/cel/grafana-provisioning/
```

`meter_mappings.yaml` is auto-discovered from the delivered files, so there is
nothing to copy on a first install.

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
4. Create the `cel-network` bridge network first, if it does not exist:
   **Networks** → **Add network**, name `cel-network`, driver `bridge` (or
   `docker network create cel-network`). Both this stack and the cloudflared
   stack attach to it, and neither owns it — if it is missing, both fail loudly
   with "network not found".
5. Copy the stack configuration:

**Use the stack configuration from [`docker-compose.yml`](docker-compose.yml)** --
copy that file's contents verbatim into the Portainer editor.

This guide used to inline its own copy, which silently drifted from the real file
and was missing services and mounts the running system depends on. Deploying that
stale copy would have quietly reintroduced fixed bugs, so it is now a pointer.

Three notes when pasting:

- There is no `version:` key. Compose v2 ignores it, and the file relies on
  `condition: service_completed_successfully`, which predates the old 3.x schema.
- Grafana's admin password is **not** set via environment variables. Set it in
  Grafana's UI on first login; see the comment in `docker-compose.yml` for why.
- Deploy over SSH or the LAN IP, **never through the Cloudflare tunnel**.
  `questdb-init` pip-installs and verifies the schema before the parser is
  allowed to start, which can exceed the tunnel's ~100s origin timeout — a 502
  there is the gateway giving up on a deploy that is still running.

6. Click **Deploy the stack**
7. Wait 2-3 minutes for containers to start

## 4. Verify (2 minutes)

Check containers in Portainer → **Containers**:

- ✅ `cel-questdb` - running
- ✅ `cel-grafana` - running
- ✅ `cel-parser` - running
- ⏹️ `cel-questdb-init` - **exited 0**. This one is supposed to stop: it applies
  and verifies the QuestDB schema, then exits. A non-zero exit means the schema
  is wrong and `cel-parser` will not start at all -- check its logs before
  anything else. Do not work around it by starting the parser manually: ingesting
  into an unverified schema can auto-create a table without DEDUP.

Open Grafana: `http://192.168.1.133:3000`
- Log in with the admin password you set (the stack does not preset one)

## 5. Test with FTP (5 minutes)

Use a **real delivered file** rather than a hand-written one: the parser reads
ValidatedMeteredData_1.6 (E66) / AggregatedMeteredData_1.3 (E31) and rejects
anything else, and the filename's `YYYYMMDD` prefix is what orders the batches.
Take one from `input/` or from the archive.

```bash
ftp 192.168.1.133
# Login with Synology credentials
put 20260806_094741_..._E66_....xml
quit
```

Check processing in Portainer:
- **Containers** → `cel-parser` → **Logs**
- Should see: `Successfully processed <filename>`
- `Skipped by design: N` is expected, not an error — a mapped virtual meter's
  production total duplicates its physical meter's, so ~9 files per delivery are
  dropped and archived deliberately

Check the data landed:
```bash
ssh admin@192.168.1.133
sudo docker exec cel-parser python3 -c \
  "import os, psycopg; print(psycopg.connect(os.environ['QUESTDB_DSN']).execute(
      'SELECT count() FROM cel_energy'
  ).fetchone())"
```

Then open a dashboard in Grafana — **CEL Energy Overview** is the default home
dashboard.

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
3. Written to QuestDB
4. Archived to `/volume1/docker/cel/archive/`

A file whose write fails is **not** archived: it stays in `/volume1/ftproot` and
is retried, so nothing is filed away having stored no data.

## Troubleshooting

### Parser not processing files

Check logs:
```bash
# Via SSH
tail -f /volume1/docker/cel/logs/watcher.log

# Via Portainer
Containers → cel-parser → Logs
```

### Parser container will not start

It waits for `cel-questdb-init` to exit 0, so check that first:
```bash
ssh admin@192.168.1.133
sudo docker logs cel-questdb-init
```
A non-zero exit means the live schema does not match `scripts/questdb_schema.sql`.
The parser refuses to start rather than write into a table without the dedup keys.

### No data in Grafana

QuestDB's ports are deliberately unpublished, so query it from inside the
network:
```bash
ssh admin@192.168.1.133
sudo docker exec -it cel-parser python3 \
    /app/scripts/validate_daily_balance_questdb.py 20260610
```

If rows exist but a panel is blank, the cause is usually the Grafana plugin
rather than the data — see [QUESTDB.md](QUESTDB.md#grafana).

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

- Overview and operations: [README.md](README.md)
- Storage schema and dedup rules: [QUESTDB.md](QUESTDB.md)
- File types, product codes, data quality: [PARSING_GUIDE.md](PARSING_GUIDE.md)
- Updating an existing deployment: [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)
