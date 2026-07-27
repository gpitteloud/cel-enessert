#!/usr/bin/env python3
"""Write samples to VictoriaMetrics so that the LATEST delivered value wins.

Why this exists
---------------
VictoriaMetrics has no per-timestamp overwrite. Given two samples with identical
``(metric_name, labels, timestamp)`` it keeps the one with the **maximum value**
on ingestion, and on query it collapses only samples inside one
``-dedup.minScrapeInterval`` window. Neither rule means "newest wins".

That matters here because the provider re-delivers each 15-min slot in 5-7
overlapping daily files and *revises* ~2.6% of them -- sometimes **downward**
(e.g. meter 0050170B, 2026-05-22T00:00: 0.003 on delivery 20260527 -> 0.002 on
20260605). Plain re-importing therefore either multiplies the energy (no dedup
flag) or pins it to the highest value ever delivered (dedup flag), overstating
totals by ~1.15%.

How it works
------------
A local SQLite file is the source of truth for "what the newest delivery says".
For each incoming batch every sample is classified against it:

* **unchanged** -> not sent at all (most samples; the overlap is redundant)
* **new**       -> sent normally via /api/v1/import
* **changed**   -> the series is marked dirty

A dirty series cannot be fixed by writing to it, so it is **deleted from VM and
re-imported in full** from the local store. Deleting is per-series (VM has no
time-range delete), which is why the full history must be replayed -- cheap here
because one series is a few thousand samples.

Net effect: VM ends up with exactly one sample per (series, timestamp), holding
the value from the most recent delivery that mentioned it. The
``--dedup.minScrapeInterval`` flag is then belt-and-braces rather than
load-bearing (it still protects data ingested before this module existed).

Ordering requirement
--------------------
"Newest" is decided by the ``delivery`` string passed in (the YYYYMMDD filename
prefix), NOT by processing order, so replaying deliveries out of order is safe:
an older delivery can never clobber a newer one.
"""

import json
import logging
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import requests

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path('/data/state/vm_samples.db')

# Re-importing a rewritten series in one request keeps the delete->import window
# short; VM accepts many values/timestamps per NDJSON line.
REWRITE_CHUNK = 20000


def _labels_key(metric: Dict[str, str]) -> str:
    """Canonical, stable identity for a series (sorted labels as JSON)."""
    return json.dumps(metric, sort_keys=True, separators=(',', ':'))


def _escape(value: str) -> str:
    """Escape a label value for a PromQL selector."""
    return value.replace('\\', '\\\\').replace('"', '\\"')


def selector_for(metric: Dict[str, str]) -> str:
    """Build an exact PromQL selector matching only this one series."""
    name = metric.get('__name__', '')
    pairs = [f'{k}="{_escape(v)}"' for k, v in sorted(metric.items())
             if k != '__name__']
    return f"{name}{{{','.join(pairs)}}}" if pairs else name


class SampleStore:
    """Local record of the newest delivered value per (series, timestamp)."""

    def __init__(self, path: Path = DEFAULT_STATE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS series (
                series_id INTEGER PRIMARY KEY,
                labels    TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS samples (
                series_id INTEGER NOT NULL,
                ts        INTEGER NOT NULL,
                value     REAL    NOT NULL,
                delivery  TEXT    NOT NULL,
                PRIMARY KEY (series_id, ts)
            ) WITHOUT ROWID;
            """
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

    def series_id(self, metric: Dict[str, str]) -> int:
        key = _labels_key(metric)
        cur = self.conn.execute(
            'SELECT series_id FROM series WHERE labels = ?', (key,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur = self.conn.execute(
            'INSERT INTO series (labels) VALUES (?)', (key,))
        return cur.lastrowid

    def existing(self, series_id: int, timestamps: Iterable[int]) -> Dict[int, Tuple[float, str]]:
        """Return {ts: (value, delivery)} for the timestamps already stored."""
        out = {}
        ts_list = list(timestamps)
        for i in range(0, len(ts_list), 500):     # keep SQL variables bounded
            chunk = ts_list[i:i + 500]
            marks = ','.join('?' * len(chunk))
            rows = self.conn.execute(
                f'SELECT ts, value, delivery FROM samples '
                f'WHERE series_id = ? AND ts IN ({marks})',
                (series_id, *chunk))
            for ts, value, delivery in rows:
                out[ts] = (value, delivery)
        return out

    def put(self, series_id: int, ts: int, value: float, delivery: str):
        self.conn.execute(
            'INSERT INTO samples (series_id, ts, value, delivery) '
            'VALUES (?, ?, ?, ?) '
            'ON CONFLICT(series_id, ts) DO UPDATE SET value = excluded.value, '
            'delivery = excluded.delivery',
            (series_id, ts, value, delivery))

    def all_samples(self, series_id: int) -> List[Tuple[int, float]]:
        rows = self.conn.execute(
            'SELECT ts, value FROM samples WHERE series_id = ? ORDER BY ts',
            (series_id,))
        return list(rows)

    def labels_for(self, series_id: int) -> Dict[str, str]:
        row = self.conn.execute(
            'SELECT labels FROM series WHERE series_id = ?', (series_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def commit(self):
        self.conn.commit()


def _post_import(points: List[Dict], vm_url: str) -> bool:
    if not points:
        return True
    url = f"{vm_url.rstrip('/')}/api/v1/import"
    body = '\n'.join(json.dumps(p) for p in points)
    try:
        resp = requests.post(url, data=body,
                             headers={'Content-Type': 'application/json'},
                             timeout=60)
    except requests.exceptions.RequestException as e:
        logger.error(f"Import request failed: {e}")
        return False
    if resp.status_code != 204:
        logger.error(f"VM import returned {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def _delete_series(metric: Dict[str, str], vm_url: str) -> bool:
    """Delete one series from VM. Required before rewriting a revised series:
    writing again would leave the old sample in place (ingest dedup keeps the
    max value), and VM offers no per-timestamp delete."""
    url = f"{vm_url.rstrip('/')}/api/v1/admin/tsdb/delete_series"
    params = urllib.parse.urlencode({'match[]': selector_for(metric)})
    try:
        resp = requests.post(f"{url}?{params}", timeout=60)
    except requests.exceptions.RequestException as e:
        logger.error(f"Delete request failed: {e}")
        return False
    if resp.status_code not in (200, 204):
        logger.error(f"VM delete returned {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def upsert(data_points: List[Dict], vm_url: str, delivery: str,
           store: SampleStore) -> Dict[str, int]:
    """Send `data_points` so the newest delivery's value is what VM holds.

    `data_points` are VM NDJSON dicts: {"metric": {...}, "values": [...],
    "timestamps": [...]}. `delivery` is the YYYYMMDD delivery prefix; a sample is
    only accepted if its delivery is >= the stored one, so out-of-order replays
    cannot regress a value.

    Returns counters: new / unchanged / revised / rewritten_series / failed.
    """
    stats = {'new': 0, 'unchanged': 0, 'revised': 0,
             'rewritten_series': 0, 'failed': 0, 'stale': 0}

    # Group incoming samples by series so each series is examined once.
    by_series: Dict[int, Dict[int, float]] = {}
    metrics: Dict[int, Dict[str, str]] = {}
    for point in data_points:
        metric = point.get('metric')
        values = point.get('values')
        timestamps = point.get('timestamps')
        if not metric or not values or not timestamps:
            logger.error(f"Invalid data point skipped: {point}")
            stats['failed'] += 1
            continue
        sid = store.series_id(metric)
        metrics[sid] = metric
        slot = by_series.setdefault(sid, {})
        for ts, value in zip(timestamps, values):
            # Within one delivery, a later line for the same slot wins.
            slot[int(ts)] = float(value)

    to_import: List[Dict] = []
    dirty: List[int] = []

    for sid, slots in by_series.items():
        prior = store.existing(sid, slots.keys())
        for ts, value in slots.items():
            if ts not in prior:
                store.put(sid, ts, value, delivery)
                to_import.append({'metric': metrics[sid],
                                  'values': [value], 'timestamps': [ts]})
                stats['new'] += 1
                continue

            old_value, old_delivery = prior[ts]
            if old_delivery > delivery:
                # A newer delivery already spoke for this slot; never regress.
                stats['stale'] += 1
                continue
            if old_value == value:
                # Redundant overlap: already correct in VM, don't resend it.
                stats['unchanged'] += 1
                if old_delivery < delivery:
                    store.put(sid, ts, value, delivery)
                continue

            # Genuine revision: VM cannot be corrected in place.
            store.put(sid, ts, value, delivery)
            stats['revised'] += 1
            if sid not in dirty:
                dirty.append(sid)

    # Plain appends first: cheap and independent of the rewrites.
    if not _post_import(to_import, vm_url):
        stats['failed'] += len(to_import)
        stats['new'] = 0

    for sid in dirty:
        metric = metrics.get(sid) or store.labels_for(sid)
        if not _delete_series(metric, vm_url):
            stats['failed'] += 1
            continue
        samples = store.all_samples(sid)
        ok = True
        for i in range(0, len(samples), REWRITE_CHUNK):
            chunk = samples[i:i + REWRITE_CHUNK]
            payload = [{'metric': metric,
                        'values': [v for _ts, v in chunk],
                        'timestamps': [ts for ts, _v in chunk]}]
            if not _post_import(payload, vm_url):
                ok = False
                break
        if ok:
            stats['rewritten_series'] += 1
            logger.info(f"Rewrote revised series ({len(samples)} samples): "
                        f"{selector_for(metric)[:120]}")
        else:
            # The series was deleted but not fully restored -- loud, because the
            # local store still holds the truth and a rerun will repair it.
            stats['failed'] += 1
            logger.error(f"Series deleted but rewrite FAILED, rerun to repair: "
                         f"{selector_for(metric)[:120]}")

    store.commit()
    return stats
