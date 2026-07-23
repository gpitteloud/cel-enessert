#!/usr/bin/env python3
"""
Validate the daily CEL energy balance from VictoriaMetrics (stored data).

This is the VictoriaMetrics counterpart of validate_daily_balance_sdat.py:
that script sums the source SDAT XML files, this one sums what actually landed
in the time-series database. Running both confirms the ingest pipeline stored
the data faithfully.

In a closed energy community, over any day the electricity CONSUMED FROM CEL
(local import) equals the electricity PRODUCED TO CEL (local export).

Checks (for the CEL-local VSE code 2404050010123):

  E66 (individual meters):
      sum cel_energy_kwh{segment="cel",direction="consumption"}
        == sum cel_energy_kwh{segment="cel",direction="production"}

  E31 (community aggregate):
      sum cel_community_energy_kwh{segment="cel",direction="consumption"}
        == sum cel_community_energy_kwh{segment="cel",direction="production"}

  Cross-check: E66 consumption sum == E31 consumption aggregate.

The "day" is a UTC calendar day [YYYYMMDD 00:00:00Z, +24h). Each stored sample
is a 15-min interval kWh value, so summing the day's samples gives daily kWh.

Usage:
    python3 validate_daily_balance_vm.py YYYYMMDD [VM_URL]

    # inside container (default URL http://victoriametrics:8428):
    python3 /app/scripts/validate_daily_balance_vm.py 20260610
    # from laptop/LAN:
    python3 scripts/validate_daily_balance_vm.py 20260610 http://192.168.1.133:8428
"""

import sys
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

DEFAULT_VM_URL = 'http://victoriametrics:8428'
CEL_LOCAL = '2404050010123'      # VSE code: energy exchanged within the community
# Tolerance: breakdown data is estimated (condition 21); allow small rounding drift
TOLERANCE_KWH = 1.0
TOLERANCE_PCT = 0.5


def day_range_utc(date_str: str):
    """Return (start_epoch, end_epoch) for the UTC calendar day YYYYMMDD."""
    start = datetime(int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8]),
                     tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def vm_export_sum(vm_url: str, match: str, start_epoch: int, end_epoch: int):
    """Export raw samples for a series selector over a time range and return
    (sum_of_values, sample_count, series_count).

    Uses /api/v1/export which streams newline-delimited JSON, one line per
    series: {"metric":{...},"values":[...],"timestamps":[...]}.
    """
    params = urllib.parse.urlencode({
        'match[]': match,
        'start': start_epoch,
        'end': end_epoch,
    })
    url = f"{vm_url.rstrip('/')}/api/v1/export?{params}"

    total = 0.0
    samples = 0
    series = 0
    with urllib.request.urlopen(url, timeout=60) as resp:
        for raw in resp:
            line = raw.decode('utf-8').strip()
            if not line:
                continue
            obj = json.loads(line)
            values = obj.get('values', [])
            series += 1
            samples += len(values)
            total += sum(values)
    return total, samples, series


def check(label, cons, prod, cons_meta, prod_meta):
    diff = cons - prod
    base = max(abs(cons), abs(prod), 1e-9)
    pct = abs(diff) / base * 100
    ok = abs(diff) <= TOLERANCE_KWH or pct <= TOLERANCE_PCT
    status = "PASS" if ok else "FAIL"
    print(f"  {label}")
    print(f"    Consumed from CEL: {cons:10.3f} kWh  ({cons_meta[0]} samples / {cons_meta[1]} series)")
    print(f"    Produced to CEL:   {prod:10.3f} kWh  ({prod_meta[0]} samples / {prod_meta[1]} series)")
    print(f"    Difference:        {diff:+10.3f} kWh  ({pct:.3f}%)   [{status}]")
    return ok, cons, prod


def validate(date_str: str, vm_url: str):
    start, end = day_range_utc(date_str)

    print(f"=== CEL daily balance validation for {date_str} (VictoriaMetrics) ===")
    print(f"  UTC window: {datetime.fromtimestamp(start, timezone.utc)} .. "
          f"{datetime.fromtimestamp(end, timezone.utc)}")
    print(f"  VM: {vm_url}")
    print()

    try:
        # E66 individual meters (segment=cel = local CEL exchange)
        e66_cons, cs, cser = vm_export_sum(
            vm_url,
            f'cel_energy_kwh{{segment="cel",direction="consumption",product_code="{CEL_LOCAL}"}}',
            start, end)
        e66_prod, ps, pser = vm_export_sum(
            vm_url,
            f'cel_energy_kwh{{segment="cel",direction="production",product_code="{CEL_LOCAL}"}}',
            start, end)

        # E31 community aggregate
        e31_cons, ecs, ecser = vm_export_sum(
            vm_url,
            f'cel_community_energy_kwh{{segment="cel",direction="consumption",product_code="{CEL_LOCAL}"}}',
            start, end)
        e31_prod, eps, epser = vm_export_sum(
            vm_url,
            f'cel_community_energy_kwh{{segment="cel",direction="production",product_code="{CEL_LOCAL}"}}',
            start, end)
    except Exception as e:
        print(f"  ERROR querying VictoriaMetrics: {e}")
        return 3

    results = []

    have_e66 = (cs + ps) > 0
    have_e31 = (ecs + eps) > 0

    if have_e66:
        ok, _, _ = check("E66 (individual meters):", e66_cons, e66_prod,
                         (cs, cser), (ps, pser))
        results.append(ok)
        print()
    else:
        print("  E66: no CEL-local samples found for this day\n")

    if have_e31:
        ok, _, _ = check("E31 (community aggregate):", e31_cons, e31_prod,
                         (ecs, ecser), (eps, epser))
        results.append(ok)
        print()
    else:
        print("  E31: no CEL-local samples found for this day\n")

    # Cross-check E66 vs E31 (consumption side)
    if have_e66 and have_e31:
        diff = e66_cons - e31_cons
        base = max(abs(e66_cons), abs(e31_cons), 1e-9)
        pct = abs(diff) / base * 100
        ok = abs(diff) <= TOLERANCE_KWH or pct <= TOLERANCE_PCT
        results.append(ok)
        print("  Cross-check E66 vs E31 (consumed from CEL):")
        print(f"    E66 sum: {e66_cons:10.3f} kWh")
        print(f"    E31 agg: {e31_cons:10.3f} kWh")
        print(f"    Difference: {diff:+10.3f} kWh  ({pct:.3f}%)   [{'PASS' if ok else 'FAIL'}]")
        print()

    if not results:
        print("No data in VictoriaMetrics for this day.")
        return 2

    all_ok = all(results)
    print("=" * 50)
    print(f"OVERALL: {'PASS - CEL energy balances' if all_ok else 'FAIL - imbalance detected'}")
    return 0 if all_ok else 1


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    date_str = sys.argv[1]
    vm_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_VM_URL
    sys.exit(validate(date_str, vm_url))
