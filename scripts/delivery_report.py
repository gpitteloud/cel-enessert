#!/usr/bin/env python3
"""What a delivery contained, and where it does not add up.

Everything is compared **per observation, inside one report period**. A delivery
is not one report period -- 20260605 carries a whole month next to a 5-day window
-- so a single sum compares a month of consumption against 5 days of production
and reports an imbalance that is only the grouping. Per slot that artefact cannot
arise, and the answer is more useful anyway: how many slots differ, by how much
in total, and which slot is worst.

Nothing here is fatal, and the exit code stays driven by ingestion failures
alone. `cel + grid = total` is already false at source for most of June -- in
20260610, 21 of 22 consumption meters report a non-zero total with both
breakdowns 0.000 -- so failing on it would abort every replay of that window.

The report reproduces what was *stored*: a paired production meter's production
total is left out and its breakdown counted under the consumption meter, exactly
as parse_e66 decides it.

It is also where the provider's declaration is checked. Ingestion trusts
meters.yaml as a per-file lookup, so a wrong pair would misattribute in silence;
here the two files of a declared pair are compared value by value -- the equality
that used to *derive* the pairing now only has to confirm it. A pair whose files
are not both in the delivery is not reported: waves arrive late, and that is
normal rather than a finding.

Usage (a delivery still in incoming, or already archived):
    python3 -m scripts.delivery_report 20260527 input/all
    docker exec cel-parser python3 -m scripts.delivery_report 20260807
"""
import logging
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from scripts.logger_config import configure_logging
from scripts.meters import Meters, load_meters
from scripts.models import is_production_breakdown, is_production_total
from scripts.parse_sdat_e66_individual import (
    consumption_on_a_production_meter, duplicates_the_consumption_total)
from scripts.sdat_header import FileHeader, parse_header

logger = logging.getLogger(__name__)

ZERO = Decimal('0')

# Enough colliding keys to see the shape of an overlap; 20260605's monthly window
# overlaps its 5-day one on two whole days, so logging them all is thousands of lines.
MAX_REPORTED_COLLISIONS = 5

# Where the declared meters are, for a standalone run: the container path first,
# then the checkout's own config/. Only the CLI looks them up -- ingestion passes
# in the ones it used, so the report can never describe a different declaration.
METERS_FILES = (Path('/app/config/meters.yaml'),
                Path(__file__).resolve().parent.parent / 'config' / 'meters.yaml')

Series = Tuple[str, str]        # direction, segment
Slots = Dict[str, Decimal]      # ISO-8601 slot -> value


@dataclass
class SeriesGap:
    """The E31 aggregate against the E66 sum, for one (direction, segment)."""
    series: Series
    shared: int = 0
    mismatching: int = 0
    difference: Decimal = ZERO      # signed sum(E31 - E66) over the shared slots
    worst: Decimal = ZERO           # largest single-slot difference, signed
    worst_at: Optional[str] = None
    e66_only: int = 0
    e31_only: int = 0


@dataclass
class BreakdownCheck:
    """`cel + grid = total` for one meter and direction, checked per slot."""
    meter: str
    direction: str
    checked: int = 0
    mismatching: int = 0
    worst: Decimal = ZERO
    worst_at: Optional[str] = None


@dataclass
class Rewrites:
    """Rows written more than once in one run under the same dedup key.

    Two report periods of one delivery overlap (20260605 on May 30-31), so a slot
    is written twice. Identical is a no-op QuestDB skips; a differing value means
    last-write-wins decided it, and which wave is written last is not a property
    any code may assume.
    """
    identical: int = 0
    differing: List[str] = field(default_factory=list)


def group_by_report_period(headers: Iterable[FileHeader]
                           ) -> Dict[Tuple, List[FileHeader]]:
    """Group CEL files by report period. RCP files are left out: they carry only
    ebIX totals, so they have no breakdown and no production metering point."""
    groups = defaultdict(list)
    for header in headers:
        if header.rcp:
            continue
        groups[header.report_period].append(header)
    return groups


def period_label(period: Tuple) -> str:
    start, end = period
    return f"{start:%Y-%m-%d}..{end:%Y-%m-%d}" if start and end else str(period)


def attributed_meter(header: FileHeader, meters: Meters) -> Optional[str]:
    """The meter one file's rows are stored under, or None if it stores none.

    The decision parse_e66 makes, through the same predicates: a paired
    production meter's total duplicates its twin's and is dropped, a consumption
    file on a production metering point is not a member's consumption, and a
    breakdown belongs to the consumption meter.
    """
    if header.rcp or header.document_type != 'E66' or not header.file_meter_id:
        return None
    if consumption_on_a_production_meter(header, meters):
        return None
    if duplicates_the_consumption_total(header, meters):
        return None
    if is_production_breakdown(header.metric_type):
        return meters.consumption_meter_for(header.file_meter_id)
    return header.file_meter_id


def e66_slots(headers: Iterable[FileHeader], meters: Meters
              ) -> Dict[str, Dict[Series, Slots]]:
    """{meter: {series: {slot: value}}} for the per-meter rows a group stores."""
    by_meter: Dict[str, Dict[Series, Slots]] = defaultdict(
        lambda: defaultdict(dict))
    for header in headers:
        meter = attributed_meter(header, meters)
        if meter is None or header.metric_type is None:
            continue
        slots = by_meter[meter][(header.direction, header.segment)]
        for obs in header.observations:
            slots[obs.timestamp] = obs.value      # last write wins, as DEDUP does
    return by_meter


def e31_slots(headers: Iterable[FileHeader]) -> Dict[Series, Slots]:
    """{series: {slot: value}} from the community aggregate files."""
    series: Dict[Series, Slots] = defaultdict(dict)
    for header in headers:
        if header.document_type != 'E31' or header.metric_type is None:
            continue
        for obs in header.observations:
            series[(header.direction, header.segment)][obs.timestamp] = obs.value
    return series


def summed_over_meters(by_meter: Dict[str, Dict[Series, Slots]]
                       ) -> Dict[Series, Slots]:
    """The per-meter readings added up slot by slot: the E66 side to compare."""
    totals: Dict[Series, Slots] = defaultdict(dict)
    for series_slots in by_meter.values():
        for series, slots in series_slots.items():
            target = totals[series]
            for slot, value in slots.items():
                target[slot] = target.get(slot, ZERO) + value
    return totals


def compare_series(e66: Dict[Series, Slots],
                   e31: Dict[Series, Slots]) -> List[SeriesGap]:
    """Per series, the signed E31 - E66 difference slot by slot.

    Signed, never absolute: a sign flip would mean out-of-scope meters leaking
    into the E66 side, a different fault from a shortfall. Only slots present on
    both sides are differenced; a slot one side alone reports is counted rather
    than folded into the total.
    """
    gaps = []
    for series in sorted(set(e66) | set(e31)):
        left, right = e66.get(series, {}), e31.get(series, {})
        gap = SeriesGap(series=series,
                        e66_only=len(set(left) - set(right)),
                        e31_only=len(set(right) - set(left)))
        for slot in sorted(set(left) & set(right)):
            difference = right[slot] - left[slot]
            gap.shared += 1
            gap.difference += difference
            if difference:
                gap.mismatching += 1
                if abs(difference) > abs(gap.worst):
                    gap.worst, gap.worst_at = difference, slot
        gaps.append(gap)
    return gaps


def breakdown_checks(by_meter: Dict[str, Dict[Series, Slots]]
                     ) -> List[BreakdownCheck]:
    """`cel + grid = total`, per meter and direction, slot by slot.

    Only a series reporting all three legs is checked: for a total-only one
    (every RCP meter, and any consumption-only member the provider sends no
    breakdown for) the identity cannot hold at all, so its absence is not a
    finding.
    """
    checks = []
    for meter in sorted(by_meter):
        for direction in ('consumption', 'production'):
            legs = [by_meter[meter].get((direction, segment))
                    for segment in ('cel', 'grid', 'total')]
            if any(leg is None for leg in legs):
                continue
            cel, grid, total = legs
            check = BreakdownCheck(meter=meter, direction=direction)
            for slot in sorted(set(cel) & set(grid) & set(total)):
                difference = total[slot] - (cel[slot] + grid[slot])
                check.checked += 1
                if difference:
                    check.mismatching += 1
                    if abs(difference) > abs(check.worst):
                        check.worst, check.worst_at = difference, slot
            checks.append(check)
    return checks


def local_leg(e66: Dict[Series, Slots]) -> Tuple[Decimal, Decimal]:
    """(consumed from CEL, produced to CEL) -- these must match without E31.

    Every kWh a member draws from the community was fed in by another one, so an
    asymmetry here is detectable from the per-meter files alone.
    """
    return (sum(e66.get(('consumption', 'cel'), {}).values(), ZERO),
            sum(e66.get(('production', 'cel'), {}).values(), ZERO))


def production_totals(headers: Iterable[FileHeader]) -> Dict[str, FileHeader]:
    """{meter id: the file carrying its ebIX production total} for one group."""
    totals = {}
    for header in headers:
        if is_production_total(header.metric_type) and header.file_meter_id:
            totals.setdefault(header.file_meter_id, header)
    return totals


def check_declared_pairs(headers: Iterable[FileHeader],
                         meters: Meters) -> List[str]:
    """Confirm each declared pair reports the same production total, or say how.

    This equality is what discovery used to *derive* the pairing from, and it is
    the only evidence the two ids belong to the same site. Ingestion no longer
    looks at it -- it trusts the declaration -- so it is checked once per report
    period here instead, where a whole group is in hand and both files of a pair
    can be compared slot by slot.

    A pair with only one of its two files present yields nothing: a delivery
    arrives in waves, so an absent file is normal and reporting it would bury the
    real finding in noise.
    """
    totals = production_totals(headers)
    disagreements = []
    for production, consumption in sorted(
            meters.consumption_by_production.items()):
        left, right = totals.get(production), totals.get(consumption)
        if left is None or right is None:
            continue
        if len(left.values) != len(right.values):
            disagreements.append(
                f"declared pair {production} -> {consumption}: production totals "
                f"cover {len(left.values)} and {len(right.values)} slot(s) "
                f"({left.file_name} vs {right.file_name})")
        elif left.values != right.values:
            differing = sum(1 for a, b in zip(left.values, right.values)
                            if a != b)
            disagreements.append(
                f"declared pair {production} -> {consumption}: production totals "
                f"differ on {differing} of {len(left.values)} slot(s) "
                f"({left.file_name} vs {right.file_name})")
    return disagreements


def rewritten_keys(headers: Iterable[FileHeader], meters: Meters) -> Rewrites:
    """Which dedup keys one run writes more than once, and whether values agree.

    Delivery-wide rather than per period, because the overlap is *between* two
    periods: 20260605's monthly window covers May 30-31 twice.
    """
    seen: Dict[tuple, Tuple[Decimal, str]] = {}
    rewrites = Rewrites()
    for header in headers:
        meter = attributed_meter(header, meters)
        if meter is None:
            continue
        for obs in header.observations:
            key = (obs.timestamp, meter, header.direction, header.segment,
                   header.product_code, header.community_id)
            previous = seen.get(key)
            if previous is None:
                seen[key] = (obs.value, header.file_name)
            elif previous[0] == obs.value:
                rewrites.identical += 1
            else:
                rewrites.differing.append(
                    f"{header.direction}/{header.segment} {meter} at "
                    f"{obs.timestamp}: {previous[0]} ({previous[1]}) then "
                    f"{obs.value} ({header.file_name})")
    return rewrites


def inventory(headers: Iterable[FileHeader]) -> Dict[Tuple, Dict[str, int]]:
    """Files per (domain, report period) -- the waves a delivery is made of."""
    counts: Dict[Tuple, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for header in headers:
        domain = 'RCP' if header.rcp else 'CEL'
        counts[(domain, header.report_period)][header.document_type or 'unknown'] += 1
    return counts


def report_period_group(period: Tuple, group: List[FileHeader],
                        meters: Meters) -> None:
    """Log the per-slot findings for one report period's CEL files."""
    by_meter = e66_slots(group, meters)
    e66 = summed_over_meters(by_meter)
    e31 = e31_slots(group)

    logger.info(f"Period {period_label(period)}: {len(group)} CEL file(s), "
                f"{len(by_meter)} meter(s)")
    for disagreement in check_declared_pairs(group, meters):
        # The declaration is wrong, or two sites now report the same total. Either
        # way a breakdown is being stored under a meter it does not belong to.
        logger.error(f"  {disagreement}")

    if not e31:
        # A wave of monthly per-meter totals arrives without its aggregate.
        logger.info("  no E31 aggregate in this group, nothing to compare against")
    for gap in compare_series(e66, e31) if e31 else []:
        direction, segment = gap.series
        if not gap.shared:
            logger.warning(
                f"  {direction}/{segment}: no slot in both E66 and E31 "
                f"({gap.e66_only} E66-only, {gap.e31_only} E31-only)")
            continue
        worst = (f", worst {gap.worst:+} at {gap.worst_at}"
                 if gap.mismatching else "")
        logger.info(f"  {direction}/{segment}: {gap.shared} shared slot(s), "
                    f"{gap.mismatching} differ, E31-E66 {gap.difference:+}{worst}")
        if gap.e66_only or gap.e31_only:
            logger.warning(f"    {gap.e66_only} slot(s) only in E66, "
                           f"{gap.e31_only} only in E31")

    consumed, produced = local_leg(e66)
    if consumed or produced:
        logger.info(f"  local leg: consumed from CEL {consumed}, produced to CEL "
                    f"{produced}, difference {consumed - produced:+}")

    checks = breakdown_checks(by_meter)
    offenders = [check for check in checks if check.mismatching]
    if not checks:
        logger.info("  no meter reports a cel/grid breakdown in this group")
    elif not offenders:
        logger.info(f"  cel + grid = total on all {len(checks)} "
                    f"meter/direction(s) that report a breakdown")
    else:
        logger.warning(f"  cel + grid != total on {len(offenders)} of "
                       f"{len(checks)} meter/direction(s)")
        for check in offenders:
            logger.warning(f"    {check.meter} {check.direction}: "
                           f"{check.mismatching}/{check.checked} slot(s) differ, "
                           f"worst {check.worst:+} at {check.worst_at}")


def report_delivery(delivery, headers: Iterable[FileHeader],
                    meters: Optional[Meters] = None,
                    failed: Iterable[str] = ()) -> None:
    """Log what one delivery contained and where it does not add up.

    `meters` is the declaration ingestion used, passed in rather than re-read, so
    the report can never describe an attribution the database does not have.
    """
    # Filename order is the provider's send order, which is the order the
    # colliding writes happened in.
    headers = sorted(headers, key=lambda header: header.file_name)
    meters = meters if meters is not None else Meters.empty()

    logger.info("=" * 80)
    logger.info(f"Delivery report {delivery}: {len(headers)} file(s)")
    for (domain, period), counts in sorted(inventory(headers).items(),
                                           key=lambda item: str(item[0])):
        detail = ', '.join(f"{n} {doc}" for doc, n in sorted(counts.items()))
        logger.info(f"  {domain} {period_label(period)}: {detail}")
    if failed:
        logger.warning(f"  {len(failed)} file(s) not ingested: "
                       f"{', '.join(sorted(failed))}")

    for period, group in sorted(group_by_report_period(headers).items(),
                                key=lambda item: str(item[0])):
        report_period_group(period, group, meters)

    rewrites = rewritten_keys(headers, meters)
    if rewrites.identical or rewrites.differing:
        logger.info(f"{rewrites.identical + len(rewrites.differing)} row(s) "
                    f"written twice by this delivery: {rewrites.identical} "
                    f"identical, {len(rewrites.differing)} with a new value")
    for collision in rewrites.differing[:MAX_REPORTED_COLLISIONS]:
        logger.warning(f"  last write wins: {collision}")
    logger.info("=" * 80)


def iter_day_files(delivery: str, search_dirs: Iterable):
    """(file name, XML bytes) for every file of one delivery, loose or archived.

    Both, because a delivery can be half-archived while a run is in progress. The
    loose copy is read first and a name already seen is not read again, so a file
    present in both places contributes once.
    """
    seen = set()
    for directory in search_dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue

        for path in sorted(directory.glob(f'{delivery}_*.xml')):
            if path.name in seen:
                continue
            seen.add(path.name)
            try:
                yield path.name, path.read_bytes()
            except OSError as e:
                logger.warning(f"Cannot read {path.name}: {e}")

        for archive in sorted(directory.glob(f'{delivery}*.zip')):
            try:
                with zipfile.ZipFile(archive) as zf:
                    for name in zf.namelist():
                        base = Path(name).name
                        if (not base.startswith(delivery)
                                or not base.endswith('.xml') or base in seen):
                            continue
                        seen.add(base)
                        yield base, zf.read(name)
            except (OSError, zipfile.BadZipFile) as e:
                logger.warning(f"Cannot read archive {archive.name}: {e}")


def headers_from_files(files) -> List[FileHeader]:
    """Parse (name, bytes) pairs into headers, leaving out what cannot be read."""
    headers = []
    for name, data in files:
        try:
            headers.append(parse_header(ET.fromstring(data), name))
        except (ET.ParseError, ValueError) as e:
            logger.error(f"{name}: cannot read header: {e}")
    return headers


def declared_meters() -> Meters:
    """The declared meters for a standalone run, or none if the file is absent.

    Unlike ingestion, which stops when it cannot load them, a report with no
    declaration is still worth having -- everything but the production breakdowns
    is unaffected. The gap is logged, loudly, so no number is read as complete.
    """
    for path in METERS_FILES:
        if path.exists():
            return load_meters(path)
    logger.error(f"No declared meters found ({', '.join(str(p) for p in METERS_FILES)}): "
                 f"production breakdowns will be reported as attributed nowhere")
    return Meters.empty()


def main(argv=None) -> int:
    """Report one delivery from the source XML; 2 only when nothing was found."""
    configure_logging()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2

    delivery = argv[0]
    search_dirs = argv[1:] or ['/data/incoming', '/data/archive']
    headers = headers_from_files(iter_day_files(delivery, search_dirs))
    if not headers:
        logger.error(f"No files for delivery {delivery} in "
                     f"{', '.join(str(d) for d in search_dirs)}")
        return 2

    report_delivery(delivery, headers, declared_meters())
    return 0


if __name__ == '__main__':
    sys.exit(main())
