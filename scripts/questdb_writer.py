#!/usr/bin/env python3
"""Write samples to QuestDB. The newest delivered value wins, for free.

Why this is so much smaller than vm_upsert
------------------------------------------
VictoriaMetrics has no per-timestamp overwrite: given two samples with the same
(metric, labels, timestamp) it keeps the MAXIMUM value. Since the provider
revises ~2.6% of overlapping slots and sometimes revises them *downward*, a plain
re-import pins a slot to the highest value ever delivered. vm_upsert works around
that with a local SQLite mirror, revision detection, and a whole-series
delete-then-replay -- ~290 lines plus a non-atomic rewrite window.

QuestDB's DEDUP UPSERT KEYS is genuine last-write-wins: an INSERT whose key
matches an existing row replaces it. The entire mechanism is one INSERT, and
re-delivering a byte-identical row is a no-op QuestDB detects and skips.

The one thing that does NOT come for free
-----------------------------------------
Last-write-wins has no notion of "newest delivery" -- it is literally whichever
INSERT ran last. Replaying delivery 20260527 AFTER 20260605 would overwrite 4
days with stale values. vm_upsert refused that via a stored delivery date;
QuestDB cannot (there is no conditional upsert, and DEDUP guarantees one row per
key so there is no second row to compare against).

Callers must therefore feed deliveries in chronological order. That already
holds: the watcher batches per delivery date and flushes on change, and its
startup rescan sorts by the YYYYMMDD filename prefix. Any new replay tooling
must sort explicitly -- see MIGRATION_QUESTDB.md.

Values are Decimal end-to-end (never float) so what lands in DECIMAL(12,3) is
exactly what the provider sent.
"""

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_DSN = os.environ.get(
    'QUESTDB_DSN', 'postgresql://admin:quest@questdb:8812/qdb')

E66_TABLE = 'cel_energy'
E31_TABLE = 'cel_community_energy'
LOG_TABLE = 'cel_ingest_log'

# Column order per table; must match questdb_schema.sql. The designated
# timestamp comes first, as in the schema.
E66_COLUMNS = ('ts', 'meter_id', 'direction', 'segment', 'product_code',
               'community_id', 'value', 'code_type', 'condition')
E31_COLUMNS = ('ts', 'direction', 'segment', 'product_code', 'community_id',
               'value', 'code_type', 'community_type', 'grid_area', 'condition')
LOG_COLUMNS = ('ts', 'delivery', 'file_name', 'document_type', 'rows_written',
               'outcome')


def insert_sql(table: str, columns: Sequence[str]) -> str:
    placeholders = ', '.join(['%s'] * len(columns))
    return f'INSERT INTO {table} ({", ".join(columns)}) VALUES ({placeholders})'


def _ts(iso: str) -> datetime:
    """ISO-8601 string -> aware datetime for the designated timestamp column."""
    return datetime.fromisoformat(iso.replace('Z', '+00:00'))


def rows_from_e66(parsed, attributed_meter_id: Optional[str] = None) -> List[tuple]:
    """Build cel_energy rows from a parsed E66 document.

    `attributed_meter_id` mirrors transform_to_datapoints: for a production
    breakdown it is the full ID of the physical meter the breakdown belongs to,
    so the rows are stored against that meter rather than the virtual one. The
    watcher computes it (it needs the virtual ID's prefix), so it is passed in
    rather than read off `parsed`.
    """
    # getattr tolerates SkippedDocument and None, as the transforms do.
    if not getattr(parsed, 'observations', None) or not parsed.metric_type:
        return []

    meter_id = parsed.meter_id
    if parsed.is_production_breakdown and attributed_meter_id:
        meter_id = attributed_meter_id

    return [
        (_ts(obs.timestamp), meter_id, parsed.metric_type.direction,
         parsed.metric_type.segment, parsed.product_code, parsed.community_id,
         obs.value, parsed.code_type, obs.condition)
        for obs in parsed.observations
    ]


def rows_from_e31(parsed) -> List[tuple]:
    """Build cel_community_energy rows from a parsed E31 document."""
    if not getattr(parsed, 'observations', None) or not parsed.metric_type:
        return []

    return [
        (_ts(obs.timestamp), parsed.metric_type.direction,
         parsed.metric_type.segment, parsed.product_code, parsed.community_id,
         obs.value, parsed.code_type, parsed.community_type, parsed.grid_area,
         obs.condition)
        for obs in parsed.observations
    ]


def validate_rows(rows: Sequence[tuple], columns: Sequence[str]) -> List[tuple]:
    """Reject rows that the database would silently mangle.

    A float `value` defeats the DECIMAL column -- binary rounding is precisely
    what it exists to avoid -- so that is a programming error and raises. A
    missing timestamp only affects its own row, so it is dropped and logged.
    """
    value_index = columns.index('value') if 'value' in columns else None
    checked = []
    for row in rows:
        if value_index is not None:
            value = row[value_index]
            if isinstance(value, float):
                raise TypeError(
                    f"value must be Decimal, not float ({value!r}): float loses "
                    f"the exactness DECIMAL(12,3) exists to provide")
            if value is None:
                logger.error(f"Skipping row with no value: {row}")
                continue
        if row[0] is None:
            logger.error(f"Skipping row with no timestamp: {row}")
            continue
        checked.append(row)
    return checked


class QuestDBWriter:
    """Thin INSERT wrapper holding one connection for the process lifetime."""

    def __init__(self, dsn: str = DEFAULT_DSN):
        self.dsn = dsn
        self._conn = None

    def _connect(self):
        if self._conn is None or self._conn.closed:
            import psycopg
            self._conn = psycopg.connect(self.dsn, autocommit=False)
        return self._conn

    def close(self):
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def write(self, table: str, columns: Sequence[str],
              rows: Sequence[tuple]) -> int:
        """Insert rows; DEDUP UPSERT KEYS makes the last write win.

        Returns the number of rows sent. Raises on failure so the caller can mark
        the file FAILED and retry it. Unlike the VM path there is no
        partially-applied state to repair: the insert is one transaction.
        """
        rows = validate_rows(rows, columns)
        if not rows:
            return 0

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.executemany(insert_sql(table, columns), rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return len(rows)

    def write_e66(self, parsed, attributed_meter_id: Optional[str] = None) -> int:
        return self.write(E66_TABLE, E66_COLUMNS,
                          rows_from_e66(parsed, attributed_meter_id))

    def write_e31(self, parsed) -> int:
        return self.write(E31_TABLE, E31_COLUMNS, rows_from_e31(parsed))

    def log_ingest(self, delivery: str, file_name: str, document_type: str,
                   rows_written: int, outcome: str,
                   ts: Optional[datetime] = None) -> None:
        """Record one ingestion event.

        Best-effort by design: provenance is useful but must never fail the
        ingestion it describes, so exceptions are logged and swallowed.
        """
        if ts is None:
            ts = datetime.now(timezone.utc)
        try:
            self.write(LOG_TABLE, LOG_COLUMNS,
                       [(ts, delivery, file_name, document_type, rows_written,
                         outcome)])
        except Exception as e:
            logger.warning(f"Could not write ingest log for {file_name}: {e}")
