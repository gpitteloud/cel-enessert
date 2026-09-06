#!/usr/bin/env python3
"""
Shared data models for SDAT parsing (E66 individual meters + E31 aggregates).

Using dataclasses instead of plain dicts gives attribute access (no silent
typos on string keys) and one shared shape across both document types.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Union


class MetricType(str, Enum):
    """Energy metric types, shared by E66 and E31.

    Each value is a ``direction`` x ``segment`` pair. Both are stored as columns
    (see :attr:`direction` / :attr:`segment`) so E66 and E31 rows share one
    scheme and can be filtered the same way in either table.
    """
    CONSUMPTION_TOTAL = 'consumption_total'
    CONSUMPTION_GRID = 'consumption_grid'
    CONSUMPTION_LOCAL = 'consumption_local'
    PRODUCTION_TOTAL = 'production_total'
    PRODUCTION_GRID = 'production_grid'
    PRODUCTION_LOCAL = 'production_local'

    @property
    def direction(self) -> str:
        """'consumption' | 'production' -> the stored `direction` column."""
        return self.value.split('_', 1)[0]

    @property
    def segment(self) -> str:
        """'cel' | 'grid' | 'total' -> the stored `segment` column.

        The enum uses 'local' internally (VSE terminology); the stored value uses
        'cel' to match how the community refers to its local exchange.
        """
        part = self.value.split('_', 1)[1]
        return 'cel' if part == 'local' else part

# Product codes shared across document types.
#   VSE local exchange (CEL) / VSE grid residual / ebIX total (grid + local)
_PRODUCT_METRIC = {
    ('consumption', '2404050010123'): MetricType.CONSUMPTION_LOCAL,
    ('production',  '2404050010123'): MetricType.PRODUCTION_LOCAL,
    ('consumption', '2404050010124'): MetricType.CONSUMPTION_GRID,
    ('production',  '2404050010124'): MetricType.PRODUCTION_GRID,
    ('consumption', '8716867000030'): MetricType.CONSUMPTION_TOTAL,
    ('production',  '8716867000030'): MetricType.PRODUCTION_TOTAL,
}

# E31 encodes direction as a flow characteristic rather than a metering point type.
_FLOW_TO_DIRECTION = {
    'E17': 'consumption',
    'E18': 'production',
}

# BusinessReasonType: the only thing separating the two domains that share the
# provider's folder. C40 = the CEL settlement, E88 = an RCP self-consumption
# grouping. Filenames, sender and receiver EICs are identical between them.
_REASON_TO_RCP = {
    'C40': False,
    'E88': True,
}


def classify_metric_type(direction: Optional[str], product_code: Optional[str]) -> Optional[MetricType]:
    """Map a flow direction + product code to a MetricType.

    Shared by both document types: E66 derives ``direction`` from the metering
    point type ('consumption'|'production'); E31 derives it from the flow
    characteristic via :func:`flow_to_direction`. Returns None for any
    unrecognized (direction, product_code) combination.
    """
    return _PRODUCT_METRIC.get((direction, product_code))


def flow_to_direction(flow_characteristic: Optional[str]) -> Optional[str]:
    """Map an E31 FlowCharacteristic (E17/E18) to a consumption/production direction."""
    return _FLOW_TO_DIRECTION.get(flow_characteristic)


def is_rcp(business_reason: Optional[str]) -> bool:
    """True for an RCP document, False for a CEL one.

    Raises on anything else rather than defaulting a third domain into CEL, where
    it would be summed into the community's charts. Derived from the business
    reason and not from Receiver/Role for exactly that reason: `Role == 'DEC'`
    answers False for an unknown domain instead of failing.
    """
    try:
        return _REASON_TO_RCP[business_reason]
    except KeyError:
        raise ValueError(
            f"unknown BusinessReasonType {business_reason!r}: expected one of "
            f"{sorted(_REASON_TO_RCP)}") from None


def is_production_total(metric_type: Optional[MetricType]) -> bool:
    """The ebIX production total: what a physical producer reports."""
    return metric_type is MetricType.PRODUCTION_TOTAL


def is_production_breakdown(metric_type: Optional[MetricType]) -> bool:
    """A production cel/grid split: what a virtual (or self-contained) meter reports."""
    return (metric_type is not None
            and metric_type.direction == 'production'
            and metric_type.segment in ('cel', 'grid'))


@dataclass(frozen=True)
class SkippedDocument:
    """A document that was understood but deliberately NOT ingested.

    Returned instead of ``None`` so callers can tell an *expected* drop apart
    from a *failure*. The only current case is a mapped virtual meter's ebIX
    production total, which duplicates its physical meter's total (~9 files per
    daily delivery). Without this distinction those drops were logged as
    "Could not parse (unknown type or invalid)" and left in the incoming folder,
    looking like a daily error and never clearing.
    """
    reason: str
    meter_id: Optional[str] = None


@dataclass
class Observation:
    """A single interval reading.

    ``value`` is a Decimal, not a float: it is stored in a DECIMAL(12,3) column
    for exact arithmetic, and parsing via float first would reintroduce the
    binary rounding that column exists to avoid. Built from the XML text
    directly -- Decimal(str) is exact, Decimal(float) is not.
    """
    sequence: int
    timestamp: str          # ISO-8601 string
    value: Decimal
    condition: Optional[str] = None    # e.g. "21" = estimated (E31; may be set on E66)


@dataclass
class MeteredData:
    """What one SDAT document contributes to the measurement tables.

    Holds only what is persisted, so a field that stops being written stops
    existing here. Everything else about the file -- period, resolution, metering
    point type, document ids -- lives on :class:`sdat_header.FileHeader` and is
    stored once per file rather than once per reading. ``document_type`` is the
    exception: it selects the target table.
    """
    document_type: str                       # 'E66' | 'E31'
    filename: str
    observations: List[Observation] = field(default_factory=list)

    # --- common ---
    product_code: Optional[str] = None
    community_id: Optional[str] = None
    # Classified from (direction, product_code); populated for both E66 and E31.
    metric_type: Optional[MetricType] = None
    # Which product-code element carried product_code: 'ebIXCode' | 'VSENationalCode'
    code_type: Optional[str] = None

    # --- E66 only ---
    # The meter the rows are stored under: already the physical meter for a
    # virtual meter's production breakdown, so no caller re-derives it.
    meter_id: Optional[str] = None
    rcp: bool = False  # is the meter a member of a RCP

    # --- E31 only ---
    grid_area: Optional[str] = None
    community_type: Optional[str] = None


# What a parser hands back: parsed data, an intentional skip, or a failure.
ParseResult = Union[MeteredData, SkippedDocument, None]
