#!/usr/bin/env python3
"""
Validate the daily CEL energy balance from QuestDB (stored data).

The QuestDB counterpart of validate_daily_balance_vm.py, which this replaces once
VM is retired in Phase 7. validate_daily_balance_sdat.py sums the source SDAT XML;
this one sums what actually landed in the database. Running both confirms the
ingest pipeline stored the data faithfully.

In a closed energy community, over any day the electricity CONSUMED FROM CEL
(local import) equals the electricity PRODUCED TO CEL (local export).

Checks (for the CEL-local VSE code 2404050010123):

  E66 (individual meters):
      sum(cel_energy.value) where segment='cel' and direction='consumption'
        == the same with direction='production'

  E31 (community aggregate):
      sum(cel_community_energy.value) where segment='cel', both directions

  Cross-check: E66 consumption sum == E31 consumption aggregate.

Two differences from the VM version, both deliberate:

  * Sums are exact. VM's /api/v1/export returns float64, so its sums carry
    binary rounding; QuestDB's DECIMAL(12,3) comes back through psycopg as
    decimal.Decimal and stays exact all the way to the printed number. The
    tolerances survive only because the *provider's* breakdown is estimated
    (condition 21) -- not because the arithmetic is lossy.

  * Every cel_energy read is scoped to the community. The provider delivers E66
    files for 8 meters with no <Community> element (community_id NULL) that are
    not in the E31 aggregate, so an unscoped sum() is not comparable to the E31
    figure beside it -- see MIGRATION_QUESTDB.md. Those 8 carry only the ebIX
    total and no VSE breakdown, so today they contribute nothing at
    segment='cel'; the filter is here so that stops being load-bearing.

The "day" is a UTC calendar day [YYYYMMDD 00:00:00Z, +24h). Each stored sample is
a 15-min interval kWh value, so summing the day's samples gives daily kWh.

Usage:
    python3 validate_daily_balance_questdb.py YYYYMMDD [--dsn postgresql://...]
                                                       [--community ID]

    # inside container (default DSN postgresql://admin:quest@questdb:8812/qdb):
    python3 /app/scripts/validate_daily_balance_questdb.py 20260610
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

DEFAULT_DSN = os.environ.get(
    'QUESTDB_DSN', 'postgresql://admin:quest@questdb:8812/qdb')

CEL_LOCAL = '2404050010123'      # VSE code: energy exchanged within the community
DEFAULT_COMMUNITY = '101110-002726'

# Tolerance: breakdown data is estimated (condition 21); allow small rounding
# drift. Kept identical to the VM and SDAT validators so the three agree on what
# counts as a pass.
TOLERANCE_KWH = Decimal('1.0')
TOLERANCE_PCT = Decimal('0.5')


def day_range_utc(date_str: str):
    """Return (start, end) datetimes for the UTC calendar day YYYYMMDD."""
    start = datetime(int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8]),
                     tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


# The half-open window matters: `ts < end` rather than BETWEEN, or the next day's
# 00:00 slot is counted twice, once in each day.
E66_SUM = """
SELECT coalesce(sum(value), 0), count()
FROM cel_energy
WHERE ts >= %s AND ts < %s
  AND community_id = %s
  AND product_code = %s
  AND segment = 'cel'
  AND direction = %s
"""

E31_SUM = """
SELECT coalesce(sum(value), 0), count()
FROM cel_community_energy
WHERE ts >= %s AND ts < %s
  AND community_id = %s
  AND product_code = %s
  AND segment = 'cel'
  AND direction = %s
"""


def fetch_sums(conn, community: str, start, end):
    """Return {(table, direction): (total_kwh, sample_count)} for the day."""
    out = {}
    with conn.cursor() as cur:
        for table, sql in (('e66', E66_SUM), ('e31', E31_SUM)):
            for direction in ('consumption', 'production'):
                cur.execute(sql, (start, end, community, CEL_LOCAL, direction))
                total, count = cur.fetchone()
                # sum() of a DECIMAL column is DECIMAL, but coalesce's literal 0
                # can come back as an int, so normalise rather than assume.
                out[(table, direction)] = (Decimal(total or 0), int(count))
    return out


def compare(cons: Decimal, prod: Decimal):
    """Return (difference, percent, ok) for a consumption-vs-production pair."""
    diff = cons - prod
    base = max(abs(cons), abs(prod))
    pct = (abs(diff) / base * 100) if base else Decimal(0)
    ok = abs(diff) <= TOLERANCE_KWH or pct <= TOLERANCE_PCT
    return diff, pct, ok


def report(label, cons, cons_n, prod, prod_n):
    diff, pct, ok = compare(cons, prod)
    print(f"  {label}")
    print(f"    Consumed from CEL: {cons:10.3f} kWh  ({cons_n} samples)")
    print(f"    Produced to CEL:   {prod:10.3f} kWh  ({prod_n} samples)")
    print(f"    Difference:        {diff:+10.3f} kWh  ({pct:.3f}%)   "
          f"[{'PASS' if ok else 'FAIL'}]")
    return ok


def validate(date_str: str, dsn: str, community: str) -> int:
    start, end = day_range_utc(date_str)

    print(f"=== CEL daily balance validation for {date_str} (QuestDB) ===")
    print(f"  UTC window: {start} .. {end}")
    print(f"  Community:  {community}")
    print(f"  QuestDB:    {dsn.split('@')[-1]}")
    print()

    try:
        import psycopg
        with psycopg.connect(dsn) as conn:
            sums = fetch_sums(conn, community, start, end)
    except Exception as e:
        print(f"  ERROR querying QuestDB: {e}")
        return 3

    results = []
    for table, label in (('e66', 'E66 (individual meters):'),
                         ('e31', 'E31 (community aggregate):')):
        cons, cons_n = sums[(table, 'consumption')]
        prod, prod_n = sums[(table, 'production')]
        if cons_n + prod_n == 0:
            print(f"  {table.upper()}: no CEL-local samples found for this day\n")
            continue
        results.append(report(label, cons, cons_n, prod, prod_n))
        print()

    e66_cons, e66_n = sums[('e66', 'consumption')]
    e31_cons, e31_n = sums[('e31', 'consumption')]
    if e66_n and e31_n:
        diff, pct, ok = compare(e66_cons, e31_cons)
        results.append(ok)
        print("  Cross-check E66 vs E31 (consumed from CEL):")
        print(f"    E66 sum: {e66_cons:10.3f} kWh")
        print(f"    E31 agg: {e31_cons:10.3f} kWh")
        print(f"    Difference: {diff:+10.3f} kWh  ({pct:.3f}%)   "
              f"[{'PASS' if ok else 'FAIL'}]")
        print()

    if not results:
        print("No data in QuestDB for this day.")
        return 2

    all_ok = all(results)
    print("=" * 50)
    print(f"OVERALL: "
          f"{'PASS - CEL energy balances' if all_ok else 'FAIL - imbalance detected'}")
    return 0 if all_ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('date', help='UTC day to validate, YYYYMMDD')
    parser.add_argument('--dsn', default=DEFAULT_DSN)
    parser.add_argument('--community', default=DEFAULT_COMMUNITY)
    args = parser.parse_args(argv)
    return validate(args.date, args.dsn, args.community)


if __name__ == '__main__':
    sys.exit(main())
