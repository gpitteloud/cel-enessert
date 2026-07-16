#!/usr/bin/env python3
"""
Validate the daily CEL energy balance from the SDAT XML files (source data).

See validate_daily_balance_vm.py for the equivalent check against the data
actually stored in VictoriaMetrics.

In a closed energy community, the electricity CONSUMED FROM CEL (local import)
should equal the electricity PRODUCED TO CEL (local export) over the same day,
because every kWh someone draws from the community was fed in by someone else.

This script validates that balance two ways:

  E66 (individual meters): sum over all members of
      consumption local (VSE 2404050010123)  ==  production local (VSE 2404050010123)

  E31 (community aggregate): for product 2404050010123 (CEL local),
      flow E17 (consumption)  ==  flow E18 (production)

It also cross-checks that E66 sums match the E31 aggregate.

Files are read from loose XML in a directory AND from daily zip archives
(YYYYMMDD.zip), so it works whether or not the day has been archived.

Usage:
    python3 validate_daily_balance_sdat.py YYYYMMDD [SEARCH_DIR ...]

    # container:
    python3 /app/scripts/validate_daily_balance_sdat.py 20260527 /data/incoming /data/archive
    # local:
    python3 scripts/validate_daily_balance_sdat.py 20260527 input/all
"""

import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {'rsm': 'http://www.strom.ch'}
CEL_LOCAL = '2404050010123'   # VSE code: energy exchanged within the community
# Tolerance: breakdown data is estimated (condition 21); allow small rounding drift
TOLERANCE_KWH = 1.0
TOLERANCE_PCT = 0.5


def _sum_observations(root_or_md) -> float:
    total = 0.0
    for obs in root_or_md.findall('.//rsm:Observation', NS):
        vol = obs.find('.//rsm:Volume', NS)
        if vol is not None and vol.text is not None:
            total += float(vol.text)
    return total


def classify_e66(root):
    """Return ('consumption'|'production', value) for a CEL-local E66 file, else None."""
    product = root.find('.//rsm:Product', NS)
    if product is None:
        return None
    vse = product.find('.//rsm:ID/rsm:VSENationalCode', NS)
    if vse is None or vse.text != CEL_LOCAL:
        return None

    if root.find('.//rsm:ConsumptionMeteringPoint', NS) is not None:
        return ('consumption', _sum_observations(root))
    if root.find('.//rsm:ProductionMeteringPoint', NS) is not None:
        return ('production', _sum_observations(root))
    return None


def classify_e31(root):
    """Return ('consumption'|'production', value) for a CEL-local E31 file, else None."""
    md = root.find('.//rsm:MeteringData', NS)
    if md is None:
        return None
    product = md.find('.//rsm:Product', NS)
    if product is None:
        return None
    vse = product.find('.//rsm:ID/rsm:VSENationalCode', NS)
    if vse is None or vse.text != CEL_LOCAL:
        return None
    flow = md.find('.//rsm:AggregationCriteria/rsm:FlowCharacteristic', NS)
    if flow is None:
        return None
    if flow.text == 'E17':      # consumption
        return ('consumption', _sum_observations(md))
    if flow.text == 'E18':      # production
        return ('production', _sum_observations(md))
    return None


def iter_day_files(date_str: str, search_dirs):
    """Yield (filename, xml_bytes) for every file of the given day found in the
    search dirs, both loose *.xml and inside YYYYMMDD*.zip archives."""
    seen = set()
    for d in search_dirs:
        d = Path(d)
        if not d.is_dir():
            continue

        # Loose XML files
        for f in sorted(d.glob(f'{date_str}_*.xml')):
            if f.name in seen:
                continue
            seen.add(f.name)
            try:
                yield f.name, f.read_bytes()
            except Exception as e:
                print(f"  WARN: could not read {f.name}: {e}")

        # Zip archives for this date (YYYYMMDD.zip and any YYYYMMDD_*.zip)
        for z in sorted(d.glob(f'{date_str}*.zip')):
            try:
                with zipfile.ZipFile(z, 'r') as zf:
                    for name in zf.namelist():
                        base = Path(name).name
                        if not base.startswith(date_str) or not base.endswith('.xml'):
                            continue
                        if base in seen:
                            continue
                        seen.add(base)
                        yield base, zf.read(name)
            except Exception as e:
                print(f"  WARN: could not read zip {z.name}: {e}")


def validate(date_str: str, search_dirs):
    e66_cons = e66_prod = 0.0
    e66_cons_n = e66_prod_n = 0
    e31_cons = e31_prod = 0.0
    e31_cons_n = e31_prod_n = 0
    parse_errors = 0

    for fname, data in iter_day_files(date_str, search_dirs):
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            parse_errors += 1
            continue

        if '_E66_' in fname:
            r = classify_e66(root)
            if r:
                kind, val = r
                if kind == 'consumption':
                    e66_cons += val; e66_cons_n += 1
                else:
                    e66_prod += val; e66_prod_n += 1
        elif '_E31_' in fname:
            r = classify_e31(root)
            if r:
                kind, val = r
                if kind == 'consumption':
                    e31_cons += val; e31_cons_n += 1
                else:
                    e31_prod += val; e31_prod_n += 1

    def check(label, cons, prod, cons_n, prod_n):
        diff = cons - prod
        base = max(abs(cons), abs(prod), 1e-9)
        pct = abs(diff) / base * 100
        ok = abs(diff) <= TOLERANCE_KWH or pct <= TOLERANCE_PCT
        status = "PASS" if ok else "FAIL"
        print(f"  {label}")
        print(f"    Consumed from CEL: {cons:10.3f} kWh  ({cons_n} files)")
        print(f"    Produced to CEL:   {prod:10.3f} kWh  ({prod_n} files)")
        print(f"    Difference:        {diff:+10.3f} kWh  ({pct:.3f}%)   [{status}]")
        return ok

    print(f"=== CEL daily balance validation for {date_str} ===")
    if parse_errors:
        print(f"  (skipped {parse_errors} unparseable file(s))")
    print()

    have_e66 = (e66_cons_n + e66_prod_n) > 0
    have_e31 = (e31_cons_n + e31_prod_n) > 0

    results = []
    if have_e66:
        results.append(check("E66 (individual meters):", e66_cons, e66_prod, e66_cons_n, e66_prod_n))
        print()
    else:
        print("  E66: no CEL-local files found for this day\n")

    if have_e31:
        results.append(check("E31 (community aggregate):", e31_cons, e31_prod, e31_cons_n, e31_prod_n))
        print()
    else:
        print("  E31: no CEL-local files found for this day\n")

    # Cross-check E66 vs E31 (consumption side, should also match)
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
        print("No data to validate for this day.")
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
    dirs = sys.argv[2:] if len(sys.argv) > 2 else ['/data/incoming', '/data/archive']
    sys.exit(validate(date_str, dirs))
