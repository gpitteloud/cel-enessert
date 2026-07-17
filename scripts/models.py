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
    """Energy metric types (E66)."""
    CONSUMPTION_TOTAL = 'consumption_total'
    CONSUMPTION_GRID = 'consumption_grid'
    CONSUMPTION_LOCAL = 'consumption_local'
    PRODUCTION_TOTAL = 'production_total'
    PRODUCTION_GRID = 'production_grid'
    PRODUCTION_LOCAL = 'production_local'


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

    # --- E66 only ---
    meter_id: Optional[str] = None
    metering_point_type: Optional[str] = None    # 'consumption' | 'production' | 'aggregated'
    metric_type: Optional[MetricType] = None
    code_type: Optional[str] = None              # 'ebIXCode' | 'VSENationalCode'
    is_production_breakdown: bool = False
    attributed_physical_meter: Optional[str] = None

    # --- E31 only ---
    flow_characteristic: Optional[str] = None    # 'E17' consumption | 'E18' production
    grid_area: Optional[str] = None
    community_type: Optional[str] = None
    product_code_type: Optional[str] = None      # 'ebIX' | 'VSE'
    business_reason: Optional[str] = None
    settlement_method: Optional[str] = None
    measure_unit: Optional[str] = None
