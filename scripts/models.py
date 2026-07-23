#!/usr/bin/env python3
"""
Shared data models for SDAT parsing (E66 individual meters + E31 aggregates).

Using dataclasses instead of plain dicts gives attribute access (no silent
typos on string keys) and one shared shape across both document types.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class MetricType(str, Enum):
    """Energy metric types, shared by E66 and E31.

    Each value is a ``direction`` x ``segment`` pair. Both are exposed as
    VictoriaMetrics labels (see :attr:`direction` / :attr:`segment`) so E66 and
    E31 series can be queried with one label scheme instead of six metric names.
    """
    CONSUMPTION_TOTAL = 'consumption_total'
    CONSUMPTION_GRID = 'consumption_grid'
    CONSUMPTION_LOCAL = 'consumption_local'
    PRODUCTION_TOTAL = 'production_total'
    PRODUCTION_GRID = 'production_grid'
    PRODUCTION_LOCAL = 'production_local'

    @property
    def direction(self) -> str:
        """'consumption' | 'production' -> the VM `direction` label."""
        return self.value.split('_', 1)[0]

    @property
    def segment(self) -> str:
        """'cel' | 'grid' | 'total' -> the VM `segment` label.

        The enum uses 'local' internally (VSE terminology); the label uses 'cel'
        to match how the community refers to its local exchange.
        """
        part = self.value.split('_', 1)[1]
        return 'cel' if part == 'local' else part


# Product codes shared across document types.
#   VSE local exchange (CEL) / VSE grid residual / ebIX total (grid + local)
_PRODUCT_METRIC = {
    ('consumption', '2404050010123'): MetricType.CONSUMPTION_LOCAL,
    ('production', '2404050010123'): MetricType.PRODUCTION_LOCAL,
    ('consumption', '2404050010124'): MetricType.CONSUMPTION_GRID,
    ('production', '2404050010124'): MetricType.PRODUCTION_GRID,
    ('consumption', '8716867000030'): MetricType.CONSUMPTION_TOTAL,
    ('production', '8716867000030'): MetricType.PRODUCTION_TOTAL,
}

# E31 encodes direction as a flow characteristic rather than a metering point type.
_FLOW_TO_DIRECTION = {
    'E17': 'consumption',
    'E18': 'production',
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


@dataclass
class Observation:
    """A single interval reading."""
    sequence: int
    timestamp: str          # ISO-8601 string
    value: float
    condition: Optional[str] = None    # e.g. "21" = estimated (E31; may be set on E66)


@dataclass
class MeteredData:
    """Parsed result of one SDAT document, shared by E66 and E31.

    Common fields apply to both; the E66-only and E31-only blocks are populated
    depending on document_type and default to None otherwise.
    """
    document_type: str                       # 'E66' | 'E31'
    observations: List[Observation] = field(default_factory=list)

    # --- common ---
    product_code: Optional[str] = None
    community_id: Optional[str] = None
    start: Optional[str] = None              # interval start (ISO-8601)
    end: Optional[str] = None                # interval end (ISO-8601)
    resolution_minutes: Optional[int] = None

    # --- common ---
    # Classified from (direction, product_code); populated for both E66 and E31.
    metric_type: Optional[MetricType] = None
    # Which product-code element carried product_code: 'ebIXCode' | 'VSENationalCode'
    # (the raw XML element name; E31 previously called this product_code_type
    # with shortened 'ebIX'/'VSE' values -- merged here to one shape).
    code_type: Optional[str] = None

    # --- E66 only ---
    meter_id: Optional[str] = None
    metering_point_type: Optional[str] = None    # 'consumption' | 'production'
    is_production_breakdown: bool = False
    attributed_physical_meter: Optional[str] = None

    # --- E31 only ---
    flow_characteristic: Optional[str] = None    # 'E17' consumption | 'E18' production
    grid_area: Optional[str] = None
    community_type: Optional[str] = None
    business_reason: Optional[str] = None
    settlement_method: Optional[str] = None
    measure_unit: Optional[str] = None
