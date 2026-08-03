#!/usr/bin/env python3
"""Create the QuestDB schema, then verify DEDUP and column types actually took.

Run once before any ingestion, and safe to re-run (every DDL statement is
IF NOT EXISTS). Verification is the point: a table created accidentally by an
insert would have no DEDUP and a default DECIMAL(18,3), which silently
reintroduces the duplicate-counting this migration removes. A wrong table is
worse than a missing one, so this exits non-zero rather than warn.

Usage:
    python3 scripts/questdb_init.py [--dsn postgresql://...] [--check-only]
"""
import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name('questdb_schema.sql')

DEFAULT_DSN = os.environ.get(
    'QUESTDB_DSN', 'postgresql://admin:quest@questdb:8812/qdb')

# What the schema must look like once applied. Checked, not assumed.
EXPECTED_DEDUP_KEYS = {
    'cel_energy': {
        'ts', 'meter_id', 'direction', 'segment', 'product_code', 'community_id'},
    'cel_community_energy': {
        'ts', 'direction', 'segment', 'product_code', 'community_id'},
    'cel_ingest_log': set(),          # no dedup by design
}

# (precision, scale) required for the value columns. Source data is always 3
# decimal places, so scale 3 is lossless; a smaller scale would round silently.
EXPECTED_DECIMAL = {
    'cel_energy': ('value', 12, 3),
    'cel_community_energy': ('value', 12, 3),
}


def _split_statements(sql: str):
    """Split a SQL script into statements, dropping comments and blanks.

    QuestDB's PG-wire endpoint executes one statement per call, so the script
    cannot be sent as a single blob.
    """
    without_comments = re.sub(r'--[^\n]*', '', sql)
    return [s.strip() for s in without_comments.split(';') if s.strip()]


def connect_with_retry(psycopg, dsn: str, timeout: float, interval: float = 2.0):
    """Connect, retrying until `timeout` seconds have passed.

    Needed because this runs as a compose init container: `depends_on` only waits
    for the container to start, not for QuestDB to accept PG-wire connections, so
    a single attempt loses a startup race. Retries only connection failures --
    once connected, a schema problem is reported, never retried.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            return psycopg.connect(dsn, autocommit=False)
        except Exception as e:
            if time.monotonic() >= deadline:
                logger.error(
                    f"Cannot connect to QuestDB after {attempt} attempts "
                    f"({timeout:.0f}s): {e}")
                return None
            if attempt == 1:
                logger.info(f"QuestDB not ready yet, retrying up to {timeout:.0f}s")
            time.sleep(interval)


def apply_schema(conn, sql: str) -> int:
    statements = _split_statements(sql)
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)
    conn.commit()
    return len(statements)


def table_columns(cur, table: str) -> dict:
    """{column: (type, is_upsert_key)} for one table.

    The table name is inlined rather than bound as a parameter: QuestDB cannot
    infer the type of a bind variable inside a table function and rejects
    `table_columns($1)` with "argument type mismatch ... actual: unknown". The
    name is validated first so an inlined identifier can never carry SQL -- these
    names come from EXPECTED_* above, not from input, but validating keeps that
    true if this is ever called with something else.
    """
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', table):
        raise ValueError(f"refusing to inline unsafe table name: {table!r}")
    cur.execute(
        f'SELECT "column", type, upsertKey FROM table_columns(\'{table}\')')
    return {col: (typ, bool(key)) for col, typ, key in cur.fetchall()}


def verify(conn) -> list:
    """Return a list of problems; empty means the schema is as intended."""
    problems = []
    with conn.cursor() as cur:
        cur.execute('SELECT table_name, dedup FROM tables()')
        tables = {name: dedup for name, dedup in cur.fetchall()}

        # One round trip per table, reused by both checks below.
        columns = {t: table_columns(cur, t) for t in EXPECTED_DEDUP_KEYS
                   if t in tables}

        for table, expected_keys in EXPECTED_DEDUP_KEYS.items():
            if table not in tables:
                problems.append(f"{table}: table missing")
                continue

            wants_dedup = bool(expected_keys)
            if bool(tables[table]) != wants_dedup:
                problems.append(
                    f"{table}: dedup={tables[table]!r}, expected "
                    f"{'enabled' if wants_dedup else 'disabled'}")

            if not wants_dedup:
                continue

            # upsertKey tells us which columns actually form the dedup key. A
            # missing key column means duplicate rows instead of an overwrite;
            # an extra one (condition, code_type) means double-counting.
            actual_keys = {col for col, (_, is_key) in columns[table].items()
                           if is_key}
            if actual_keys != expected_keys:
                missing = expected_keys - actual_keys
                extra = actual_keys - expected_keys
                detail = []
                if missing:
                    detail.append(f"missing={sorted(missing)}")
                if extra:
                    detail.append(f"UNEXPECTED={sorted(extra)}")
                problems.append(f"{table}: dedup keys wrong ({', '.join(detail)})")

        for table, (column, precision, scale) in EXPECTED_DECIMAL.items():
            if table not in tables:
                continue
            entry = columns[table].get(column)
            actual = entry[0] if entry else None
            expected = f'DECIMAL({precision},{scale})'
            # Compare loosely on whitespace; QuestDB may render "DECIMAL(12, 3)".
            if actual is None or actual.replace(' ', '').upper() != expected:
                problems.append(
                    f"{table}.{column}: type={actual!r}, expected {expected} "
                    f"(a DOUBLE here loses exact arithmetic; a smaller scale "
                    f"rounds values silently)")

    return problems


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn', default=DEFAULT_DSN,
                        help='QuestDB PG-wire DSN (default: $QUESTDB_DSN)')
    parser.add_argument('--check-only', action='store_true',
                        help='verify an existing schema without creating it')
    parser.add_argument('--wait', type=float, default=60.0, metavar='SECONDS',
                        help='how long to wait for QuestDB to accept connections '
                             '(default 60; 0 = single attempt)')
    args = parser.parse_args()

    try:
        import psycopg
    except ModuleNotFoundError:
        logger.error("psycopg is required: pip install 'psycopg[binary]'")
        return 2

    conn = connect_with_retry(psycopg, args.dsn, args.wait)
    if conn is None:
        return 2

    try:
        if not args.check_only:
            count = apply_schema(conn, SCHEMA_PATH.read_text())
            logger.info(f"Applied {count} statements from {SCHEMA_PATH.name}")

        problems = verify(conn)
    finally:
        conn.close()

    if problems:
        logger.error("Schema verification FAILED:")
        for problem in problems:
            logger.error(f"  - {problem}")
        logger.error(
            "Do NOT ingest into this schema. If a table was auto-created by an "
            "insert, drop it and re-run: dedup cannot be added to a non-WAL "
            "table, and enabling dedup does not deduplicate existing rows.")
        return 1

    logger.info("Schema verified: dedup keys and DECIMAL(12,3) columns correct")
    return 0


if __name__ == '__main__':
    sys.exit(main())
