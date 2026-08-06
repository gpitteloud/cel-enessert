# Deployment Checklist

**Last updated**: 2026-08-06
**Purpose**: Deploy code, config or dashboard changes to the Synology NAS

For a first-time install, use [QUICK_START_SYNOLOGY.md](QUICK_START_SYNOLOGY.md)
instead — this covers updating a stack that is already running.

---

## What goes where

| Repo path | NAS target |
|-----------|------------|
| `scripts/*.py`, `scripts/questdb_schema.sql` | `/volume1/docker/cel/scripts/` |
| `config/api_config.yaml`, `config/meter_mappings.yaml` | `/volume1/docker/cel/config/` |
| `grafana-dashboards/*.json` | `/volume1/docker/cel/grafana-dashboards/` |
| `grafana-provisioning/**` | `/volume1/docker/cel/grafana-provisioning/` |
| `docker-compose.yml` | Pasted into the Portainer stack, not copied |

Both `scripts/` and `config/` are bind-mounted into `cel-parser`, so a copied
file is visible immediately — but the running process has already imported its
modules, so **a script change needs a container restart** to take effect.

---

## Deployment Commands

### Step 1: Run the tests first

```bash
# From the repo root
python3 -m pytest tests -q
```

The suite covers the parsers, the writer's last-write-wins semantics, and the
dashboards' SQL. A dashboard change that breaks a plugin constraint fails here
rather than rendering an empty panel on the NAS.

### Step 2: Copy the changed files

```bash
# From your development machine, in /home/copadev/projects/cel/cel-community/

scp scripts/*.py scripts/questdb_schema.sql \
    synology:/volume1/docker/cel/scripts/

scp config/api_config.yaml \
    synology:/volume1/docker/cel/config/

scp grafana-dashboards/*.json \
    synology:/volume1/docker/cel/grafana-dashboards/
```

Copy only what changed. In particular, **check `config/api_config.yaml` against
the NAS copy before overwriting it** — the NAS copy is the live configuration and
may have been edited in place.

### Step 3: Restart what needs restarting

```bash
ssh synology

# Script or config change:
docker restart cel-parser
docker logs -f cel-parser

# Dashboard JSON change:  nothing. Provisioning re-reads from disk every ~10s.

# docker-compose.yml or grafana-provisioning change:  redeploy the stack in
# Portainer. Do this over SSH or the LAN IP, never through the Cloudflare
# tunnel -- questdb-init pip-installs and verifies the schema before the parser
# is allowed to start, which can exceed the tunnel's ~100s origin timeout. A 502
# there is the gateway giving up on a deploy that is still running.
```

### Step 4: Schema changes need `questdb-init`

`scripts/questdb_schema.sql` is the authoritative DDL and `questdb_init.py`
verifies the live database against it. If you changed the DDL, redeploy the stack
so `cel-questdb-init` runs again, and read its log:

```bash
docker logs cel-questdb-init
```

A non-zero exit means the live schema does not match the DDL, and the parser will
**refuse to start** rather than write into a table whose dedup keys or decimal
precision differ. That refusal is the point: an auto-created table has no DEDUP
and a default `DECIMAL(18,3)`, which silently double-counts overlapping
deliveries.

Note that `questdb_init.py` applies missing tables but does not migrate an
existing one. Changing a dedup key or a column type on a populated table means
dropping and rebuilding it, then replaying the archive **in ascending delivery
order** — see [QUESTDB.md](QUESTDB.md#chronological-replay-is-a-correctness-requirement).

---

## Verification

```bash
# Parser is alive and processing
docker logs --tail 50 cel-parser

# Rows are landing
docker exec cel-parser python3 -c \
  "import os, psycopg; print(psycopg.connect(os.environ['QUESTDB_DSN']).execute(
      'SELECT count() FROM cel_energy'
  ).fetchone())"

# A day's figures balance, source vs stored
docker exec -it cel-parser python3 \
  /app/scripts/validate_daily_balance_questdb.py 20260610
```

### Checklist

- [ ] `pytest` green locally before copying anything
- [ ] Only the intended files copied; `api_config.yaml` diffed against the NAS copy
- [ ] Parser container restarted (if scripts or config changed)
- [ ] `docker logs cel-questdb-init` exits 0 (if the schema changed)
- [ ] Logs show both E66 and E31 files being processed, no errors
- [ ] Nothing left behind in `/data/incoming` that should have been archived
- [ ] Dashboards render data (a dashboard JSON change appears within ~10s)
- [ ] Retired or renamed dashboards deleted **by hand in the Grafana UI** —
      provisioning adds and updates but never deletes

---

## Expected Logs

```
2026-08-06 14:30:01 CEST - __main__ - INFO - Processing 20260806_094741_..._E31_....xml
2026-08-06 14:30:01 CEST - __main__ - INFO - 20260806_094741_..._E31_....xml: Parsed 480 community aggregate observations
2026-08-06 14:30:02 CEST - __main__ - INFO - QuestDB: wrote 480 rows from 20260806_094741_..._E31_....xml
2026-08-06 14:30:02 CEST - __main__ - INFO - Successfully processed 20260806_094741_..._E31_....xml
2026-08-06 14:30:02 CEST - __main__ - INFO - Archived 20260806_094741_..._E31_....xml to /data/archive/...
```

`Skipped by design: 9` in the batch summary is **expected, not an error**: a
mapped virtual meter's ebIX production total duplicates its physical meter's, so
the parser drops it (~9 files per delivery). Those files are archived. Only
genuine failures stay in `/data/incoming`.

---

## Rollback

Scripts are plain files on a bind mount, so rollback is a copy:

```bash
ssh synology

docker stop cel-parser
cp /volume1/docker/cel/scripts/watch_ftproot.py.backup \
   /volume1/docker/cel/scripts/watch_ftproot.py
docker start cel-parser
```

Data written before the rollback stays written. Because the dedup keys make the
newest write win, re-running the previous code over the same deliveries simply
overwrites those slots again — provided the deliveries are replayed in ascending
date order.

---

## Troubleshooting

### Parser will not start
It waits on `cel-questdb-init` exiting 0. Check `docker logs cel-questdb-init`
first; a schema mismatch is the usual cause.

### Files pile up in `/data/incoming`
A failed write marks the file FAILED and leaves it there deliberately, so it is
retried rather than archived having stored nothing. Check `docker logs cel-parser`
for the write error, and confirm QuestDB is up: `docker ps | grep cel-questdb`.

### Import errors in the logs
```bash
docker exec cel-parser python3 -c "from parse_sdat import parse_sdat; print('OK')"
docker exec cel-parser python3 -c "from questdb_writer import QuestDBWriter; print('OK')"
```
A missing module usually means only some of `scripts/` was copied.

### Panels empty after a dashboard change
Almost always a plugin constraint rather than the data — an un-cast `DECIMAL`
column, `format` written as a string instead of the numeric enum, or a `byName`
override on a multi-frame panel. `tests/test_dashboards_sql.py` catches all
three; see [QUESTDB.md](QUESTDB.md#grafana).
