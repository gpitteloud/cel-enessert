#!/usr/bin/env python3
"""
Discover which physical meter each virtual meter's production breakdown belongs to.

Two independent steps, and only the second one looks at values:

**Classification is structural.** The set of (direction, segment) series a meter
reports over one report period says what it is, with no reference to its id:

    has_breakdown    = it reports a production cel/grid split
    has_consumption  = it reports any consumption series

    virtual          = has_breakdown and not has_consumption
    self-contained   = has_breakdown and has_consumption      -> owns its breakdown
    physical producer = reports a production total, and is not virtual

A virtual meter has no consumption file at all -- that absence is the
discriminator, and it separates the self-contained meter without a special case.

**Pairing compares the whole reading vector.** Files are grouped by report period
first, so every file in a group covers the same slots and two vectors can be
compared for exact Decimal equality: no sums, no tolerance, no sampling, and a
monthly file can never be matched against a 5-day one. A virtual meter also
reports the production total of its physical twin, identical value by value, and
that is what pairs them.

Nothing is guessed. Zero candidates, several candidates, or a physical meter
already claimed by another virtual is reported as an ambiguity, and the group is
then not trusted.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import yaml

from scripts.models import is_production_breakdown, is_production_total
from scripts.sdat_header import FileHeader

logger = logging.getLogger(__name__)

# A VSE national meter id is 33 characters. A cache written by the previous
# version keyed on the last 8, which is not a meter and must not be trusted.
MIN_FULL_METER_ID_LENGTH = 20


@dataclass
class MeterClasses:
    """What each meter is, over one report period."""
    virtual: Set[str] = field(default_factory=set)
    self_contained: Set[str] = field(default_factory=set)
    physical_producers: Set[str] = field(default_factory=set)


@dataclass
class PeriodDiscovery:
    """Discovery result for one (report period) group of CEL files."""
    period: Tuple
    classes: MeterClasses
    mappings: Dict[str, str] = field(default_factory=dict)   # virtual -> physical
    ambiguities: List[str] = field(default_factory=list)

    @property
    def self_contained(self) -> Set[str]:
        return self.classes.self_contained

    @property
    def complete(self) -> bool:
        """True when every virtual meter paired and nothing was ambiguous."""
        return (not self.ambiguities
                and len(self.mappings) == len(self.classes.virtual))


def group_by_report_period(headers: Iterable[FileHeader]) -> Dict[Tuple, List[FileHeader]]:
    """Group CEL files by report period. RCP files are left out: they carry only
    ebIX totals, so they have no breakdown and no virtual meters."""
    groups = defaultdict(list)
    for header in headers:
        if header.rcp:
            continue
        groups[header.report_period].append(header)
    return groups


def classify_meters(headers: Iterable[FileHeader]) -> MeterClasses:
    """Classify every meter in one group from the series it reports."""
    signatures = defaultdict(set)
    for header in headers:
        if header.file_meter_id and header.metric_type:
            signatures[header.file_meter_id].add(header.metric_type)

    classes = MeterClasses()
    for meter_id, metric_types in signatures.items():
        has_breakdown = any(is_production_breakdown(m) for m in metric_types)
        has_consumption = any(m.direction == 'consumption' for m in metric_types)
        if has_breakdown and not has_consumption:
            classes.virtual.add(meter_id)
        elif has_breakdown:
            classes.self_contained.add(meter_id)

    for meter_id, metric_types in signatures.items():
        if (any(is_production_total(m) for m in metric_types)
                and meter_id not in classes.virtual):
            classes.physical_producers.add(meter_id)

    return classes


def _production_totals(headers: Iterable[FileHeader]) -> Dict[str, Tuple]:
    """{meter id: reading vector} from the ebIX production total files."""
    totals = {}
    for header in headers:
        if is_production_total(header.metric_type) and header.file_meter_id:
            totals.setdefault(header.file_meter_id, header.values)
    return totals


def pair_virtual_meters(headers: List[FileHeader], classes: MeterClasses
                        ) -> Tuple[Dict[str, str], List[str]]:
    """Pair each virtual meter with the physical meter whose total it repeats.

    Returns (virtual -> physical, ambiguities). One physical meter can back only
    one virtual meter, so a second claim on it is an ambiguity rather than an
    overwrite -- the previous version never removed a matched meter from the pool
    and so could map two physicals onto the same virtual, one of them wrongly.
    """
    totals = _production_totals(headers)
    mappings: Dict[str, str] = {}
    claimed_by: Dict[str, str] = {}
    ambiguities: List[str] = []

    for virtual in sorted(classes.virtual):
        vector = totals.get(virtual)
        if vector is None:
            ambiguities.append(
                f"virtual meter {virtual} reports no production total, "
                f"nothing to pair it on")
            continue

        candidates = [physical for physical in sorted(classes.physical_producers)
                      if totals.get(physical) == vector]
        if not candidates:
            ambiguities.append(
                f"virtual meter {virtual}: no physical producer reports the same "
                f"production total")
            continue
        if len(candidates) > 1:
            ambiguities.append(
                f"virtual meter {virtual}: {len(candidates)} physical producers "
                f"report the same production total ({', '.join(candidates)})")
            continue

        physical = candidates[0]
        if physical in claimed_by:
            ambiguities.append(
                f"physical meter {physical} matches both {claimed_by[physical]} "
                f"and {virtual}")
            continue

        mappings[virtual] = physical
        claimed_by[physical] = virtual
        logger.info(f"Paired virtual {virtual} -> physical {physical}")

    return mappings, ambiguities


def discover_mappings(headers: Iterable[FileHeader]) -> Dict[Tuple, PeriodDiscovery]:
    """Discover mappings for every report period present in a batch.

    A delivery is not one report period: 20260807 carries a monthly group and a
    5-day group, whose totals must never be compared with each other.
    """
    results = {}
    for period, group in group_by_report_period(headers).items():
        classes = classify_meters(group)
        mappings, ambiguities = pair_virtual_meters(group, classes)
        result = PeriodDiscovery(period=period, classes=classes,
                                 mappings=mappings, ambiguities=ambiguities)
        results[period] = result

        logger.info(
            f"Period {_period_label(period)}: {len(group)} CEL file(s), "
            f"{len(classes.virtual)} virtual, "
            f"{len(classes.self_contained)} self-contained, "
            f"{len(classes.physical_producers)} physical producer(s), "
            f"{len(mappings)} mapping(s)")
        for ambiguity in ambiguities:
            logger.error(f"Period {_period_label(period)}: {ambiguity}")

    return results


def _period_label(period: Tuple) -> str:
    start, end = period
    return f"{start:%Y-%m-%d}..{end:%Y-%m-%d}" if start and end else str(period)


def load_cached_mappings(cache_file: Path) -> Dict[str, str]:
    """Read the recorded virtual -> physical mappings, or {} if unusable.

    The cache is a record of the last discovery, not the source of truth:
    discovery now runs from the batch itself on every run. A cache keyed on
    anything other than full meter ids was written by the previous version and is
    ignored rather than half-trusted.
    """
    cache_file = Path(cache_file)
    if not cache_file.exists():
        return {}
    try:
        data = yaml.safe_load(cache_file.read_text()) or {}
    except Exception as e:
        logger.warning(f"Cannot read mapping cache {cache_file}: {e}")
        return {}

    mappings = data.get('meter_mappings') or {}
    if not isinstance(mappings, dict):
        return {}
    if any(len(str(virtual)) < MIN_FULL_METER_ID_LENGTH
           or len(str(physical)) < MIN_FULL_METER_ID_LENGTH
           for virtual, physical in mappings.items()):
        logger.warning(f"Ignoring mapping cache {cache_file}: not keyed on full "
                       f"meter ids, so it predates the current format")
        return {}
    return dict(mappings)


def save_mappings(mappings: Dict[str, str], cache_file: Path) -> None:
    """Record the discovered mappings for the next run to compare against."""
    with open(cache_file, 'w') as f:
        f.write("# Virtual to physical meter mappings, virtual meter id first.\n")
        f.write("# A record of the last discovery -- discovery itself runs from\n")
        f.write("# the delivery on every run. DO NOT EDIT.\n\n")
        yaml.dump({'meter_mappings': dict(mappings)}, f,
                  default_flow_style=False)
    logger.info(f"Recorded {len(mappings)} mappings in {cache_file}")


def log_mapping_changes(discovered: Dict[str, str],
                        cached: Dict[str, str]) -> None:
    """Log how discovery differs from the recorded mappings.

    A new member appearing is exactly the event worth logging, and a mapping that
    *changed* would mean a breakdown moving to a different meter -- worth an error
    even though discovery, not the cache, decides.
    """
    for virtual in sorted(set(discovered) - set(cached)):
        logger.info(f"New mapping: {virtual} -> {discovered[virtual]}")
    for virtual in sorted(set(cached) - set(discovered)):
        logger.warning(f"Mapping gone: {virtual} -> {cached[virtual]}")
    for virtual in sorted(set(cached) & set(discovered)):
        if cached[virtual] != discovered[virtual]:
            logger.error(f"Mapping changed for {virtual}: "
                         f"{cached[virtual]} -> {discovered[virtual]}")


def mappings_for_period(result: PeriodDiscovery,
                        cached: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The mappings to ingest a period with: discovery's, or the record's.

    An incomplete or ambiguous group is not trusted -- a wrongly attributed
    breakdown is stored under the wrong member and cannot be told apart later --
    so the recorded mappings are used instead and the difference is logged.
    """
    if result.complete:
        return result.mappings
    logger.warning(
        f"Period {_period_label(result.period)}: discovery incomplete "
        f"({len(result.mappings)}/{len(result.classes.virtual)} virtual meters "
        f"paired), falling back to the recorded mappings")
    return dict(cached or {})
