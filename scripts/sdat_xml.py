#!/usr/bin/env python3
"""
Shared XML extraction helpers for SDAT documents (E66 + E31).

These cover the parts that are genuinely identical between the two document
types. Everything left inline in the parsers is type-specific (E66 meter
attribution, E31 flow/community metadata) and reads more clearly there.

All helpers operate on an already-parsed <MeteringData> element and the shared
``ns`` namespace map; they never touch files.
"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

from models import Observation

# Scale of the DECIMAL(12,3) column the values land in. Every observation in the
# 3297-file sample corpus has exactly 3 decimal places, so this is lossless --
# but a 4-dp value would be rounded silently by the database, so reject it here
# instead. If the provider ever changes resolution this fires loudly and the
# column scale must be widened before ingesting.
VALUE_SCALE = 3


def extract_product_code(metering_data, ns) -> Tuple[Optional[str], Optional[str]]:
    """Return (product_code, code_type) from a MeteringData element.

    Prefers the ebIX code, falls back to the VSE national code. code_type is
    the raw element name ('ebIXCode' | 'VSENationalCode'). Returns (None, None)
    when no product code is present.
    """
    for code_type in ('ebIXCode', 'VSENationalCode'):
        elem = metering_data.find(f'.//rsm:Product/rsm:ID/rsm:{code_type}', ns)
        if elem is not None:
            return elem.text, code_type
    return None, None


def extract_resolution_minutes(metering_data, ns) -> Optional[int]:
    """Return the interval resolution in minutes, or None if absent/not in MIN.

    Callers treat None as fatal: a document without a usable resolution cannot
    be turned into timestamped observations.
    """
    resolution = metering_data.find('.//rsm:Resolution', ns)
    if resolution is None:
        return None
    value = resolution.find('rsm:Resolution', ns)
    unit = resolution.find('rsm:Unit', ns)
    if value is not None and unit is not None and unit.text == 'MIN':
        return int(value.text)
    return None


def parse_observations(metering_data, ns, start_iso: str, resolution_minutes: int) -> List[Observation]:
    """Parse <Observation> elements into a list of Observation.

    Each observation's timestamp is derived from the interval start plus
    (sequence - 1) * resolution. Observations missing a Sequence or Volume
    element are skipped; a malformed numeric value raises (ValueError), which
    the parser's outer handler treats as a fatal document error rather than
    silently ingesting partial data.

    Volumes are parsed as Decimal straight from the XML text (never via float)
    so the value stored in the DECIMAL(12,3) column is exactly what the provider
    sent. A value with more than VALUE_SCALE decimal places raises ValueError
    rather than let the database round it away unnoticed.
    """
    base_dt = datetime.fromisoformat(start_iso.replace('Z', '+00:00'))
    observations = []
    for obs in metering_data.findall('.//rsm:Observation', ns):
        seq_elem = obs.find('.//rsm:Position/rsm:Sequence', ns)
        vol_elem = obs.find('.//rsm:Volume', ns)
        if seq_elem is None or vol_elem is None:
            continue

        sequence = int(seq_elem.text)
        raw_volume = (vol_elem.text or '').strip()
        try:
            volume = Decimal(raw_volume)
        except InvalidOperation:
            # Match float()'s contract: the outer handler treats ValueError as a
            # fatal document error instead of ingesting partial data.
            raise ValueError(f"Invalid Volume value: {raw_volume!r}")
        if -volume.as_tuple().exponent > VALUE_SCALE:
            raise ValueError(
                f"Volume {raw_volume!r} has more than {VALUE_SCALE} decimal "
                f"places; DECIMAL(12,{VALUE_SCALE}) would round it silently")
        cond_elem = obs.find('.//rsm:Condition', ns)
        obs_dt = base_dt + timedelta(minutes=(sequence - 1) * resolution_minutes)

        observations.append(Observation(
            sequence=sequence,
            value=volume,
            timestamp=obs_dt.isoformat(),
            condition=cond_elem.text if cond_elem is not None else None,
        ))
    return observations
