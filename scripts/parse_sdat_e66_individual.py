#!/usr/bin/env python3
"""
E66 (ValidatedMeteredData_1.6) -> MeteredData: individual meter readings.

The XML is already read into a FileHeader (see sdat_header); what is left here is
the one decision E66 needs and E31 does not -- which meter a production breakdown
belongs to. That decision is *finished* here, so MeteredData.meter_id is the id
the rows are stored under and no later stage re-derives it.
"""
import logging
from typing import Optional

from scripts.models import (MeteredData, ParseResult, SkippedDocument,
                            is_production_breakdown, is_production_total)
from scripts.sdat_header import FileHeader

logger = logging.getLogger(__name__)


def duplicates_a_physical_total(header: FileHeader, meter_mappings: dict) -> bool:
    """True for a mapped virtual meter's production total.

    It is identical to its physical meter's total -- that equality is how the two
    are paired in the first place -- so storing both would double the community's
    production. A self-contained meter is not in the mappings, so it keeps its own
    total.
    """
    return (is_production_total(header.metric_type)
            and header.file_meter_id in (meter_mappings or {}))


def resolve_production_owner(meter_id: str, meter_mappings: dict,
                             self_contained_meters: set) -> Optional[str]:
    """The physical meter a production breakdown belongs to, or None if unknown.

    A separate virtual meter's breakdown goes to its mapped physical twin; a
    self-contained meter carries the breakdown on the same id as its total and
    owns it. Unknown means a new member: nothing is guessed.
    """
    physical = (meter_mappings or {}).get(meter_id)
    if physical:
        return physical
    if meter_id in (self_contained_meters or set()):
        return meter_id
    return None


def parse_e66(header: FileHeader, meter_mappings: dict = None,
              self_contained_meters: set = None) -> ParseResult:
    """
    Turn an E66 FileHeader into the rows it should be stored as.

    Args:
        header: the parsed file, observations included
        meter_mappings: virtual meter id -> physical meter id (full ids)
        self_contained_meters: meter ids carrying their own production breakdown

    Returns:
        MeteredData with document_type='E66';
        SkippedDocument if the document is valid but deliberately not ingested
        (a mapped virtual meter's duplicate production total -- an expected,
        non-error outcome for ~9 files per delivery);
        None if the file has no meter id or its breakdown cannot be attributed.
    """
    meter_id = header.file_meter_id
    if not meter_id:
        logger.error(f"{header.file_name}: no consumption or production meter id")
        return None

    if duplicates_a_physical_total(header, meter_mappings):
        # Not an error: reported as an intentional skip so the caller logs it as
        # expected and still archives the file.
        return SkippedDocument(
            reason=(f"virtual meter {meter_id} production total duplicates "
                    f"physical {meter_mappings[meter_id]}"),
            meter_id=meter_id,
        )

    if is_production_breakdown(header.metric_type):
        owner = resolve_production_owner(
            meter_id, meter_mappings, self_contained_meters)
        if owner is None:
            logger.error(f"Unknown virtual meter {meter_id} - no mapping found "
                         f"in auto-discovery. Skipping {header.file_name}.")
            return None
        if owner != meter_id:
            logger.info(f"Virtual meter {meter_id} -> attributing production "
                        f"breakdown to physical meter {owner}")
        meter_id = owner

    if header.metric_type is None:
        logger.warning(f"{header.file_name}: unclassified product code "
                       f"{header.product_code!r}")

    # The header row records where the rows actually went, so the file's own
    # meter and the stored meter are both queryable.
    header.attributed_meter_id = meter_id

    return MeteredData(
        document_type='E66',
        filename=header.file_name,
        observations=header.observations,
        product_code=header.product_code,
        code_type=header.code_type,
        community_id=header.community_id,
        metric_type=header.metric_type,
        meter_id=meter_id,
        rcp=header.rcp,
    )
