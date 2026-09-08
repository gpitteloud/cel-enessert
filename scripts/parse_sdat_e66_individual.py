#!/usr/bin/env python3
"""
E66 (ValidatedMeteredData_1.6) -> MeteredData: individual meter readings.

The XML is already read into a FileHeader (see sdat_header); what is left here is
the one decision E66 needs and E31 does not -- which meter a production breakdown
belongs to. That decision is *finished* here, so MeteredData.meter_id is the id
the rows are stored under and no later stage re-derives it.

Every rule below is a lookup in the declared meters (see scripts/meters.py), so
one file is decidable on its own: a late or retried file needs no batch around it.
"""
import logging
from typing import Optional

from scripts.meters import Meters
from scripts.models import (MeteredData, ParseResult, SkippedDocument,
                            is_production_breakdown, is_production_total)
from scripts.sdat_header import FileHeader

logger = logging.getLogger(__name__)


def duplicates_the_consumption_total(header: FileHeader, meters: Meters) -> bool:
    """True for a paired production meter's ebIX production total.

    The consumption meter of the same member reports that total too, identically,
    so storing both would double the community's production. A production-only
    meter has no twin and keeps its own total.
    """
    return (is_production_total(header.metric_type)
            and meters.is_paired_production_meter(header.file_meter_id))


def consumption_on_a_production_meter(header: FileHeader,
                                      meters: Meters) -> bool:
    """True for a consumption file bearing a declared production metering point.

    A production metering point measures what a site feeds in; it has no
    consumption to report. The provider nonetheless sends consumption files for
    0134575W, and those readings are not a member's consumption -- see
    PROVIDER_QUESTIONS.md. Stated for every production metering point rather than
    only the production-only ones: a paired one sending consumption would be the
    same fault, and today none does, so this changes nothing for them.
    """
    return (header.metering_point_type == 'consumption'
            and meters.is_production_metering_point(header.file_meter_id))


def parse_e66(header: FileHeader,
              meters: Optional[Meters] = None) -> ParseResult:
    """
    Turn an E66 FileHeader into the rows it should be stored as.

    Args:
        header: the parsed file, observations included
        meters: the declared meters; without them a production breakdown cannot
            be attributed and its file fails

    Returns:
        MeteredData with document_type='E66';
        SkippedDocument if the document is valid but deliberately not ingested
        (a paired production meter's duplicate total, ~9 files per delivery, or a
        consumption file on a production metering point);
        None if the file has no meter id or its breakdown cannot be attributed.
    """
    meters = meters if meters is not None else Meters.empty()

    meter_id = header.file_meter_id
    if not meter_id:
        logger.error(f"{header.file_name}: no consumption or production meter id")
        return None

    # Both drops below are intentional, so they are reported as skips: the caller
    # logs them as expected and still archives the file.
    if consumption_on_a_production_meter(header, meters):
        return SkippedDocument(
            reason=(f"{meter_id} is a production metering point, so this "
                    f"consumption file is not a member's consumption"),
            meter_id=meter_id,
        )

    if duplicates_the_consumption_total(header, meters):
        return SkippedDocument(
            reason=(f"production meter {meter_id} repeats the production total "
                    f"of consumption meter "
                    f"{meters.consumption_by_production[meter_id]}"),
            meter_id=meter_id,
        )

    if is_production_breakdown(header.metric_type):
        owner = meters.consumption_meter_for(meter_id)
        if owner is None:
            logger.error(f"{describe_undeclared(meter_id, meters)}. "
                         f"Skipping {header.file_name}.")
            return None
        if owner != meter_id:
            logger.info(f"Production meter {meter_id} -> attributing production "
                        f"breakdown to consumption meter {owner}")
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


def describe_undeclared(meter_id: str, meters: Meters) -> str:
    """Why a production breakdown could not be attributed.

    A declared consumption-only member sending one is a wrong declaration; an
    unknown id is a meter we were never told about. Both need the provider, but
    not the same question, so they do not share a message.
    """
    if meters.is_consumption_only(meter_id):
        return (f"{meter_id} is declared consumption-only but reports a "
                f"production breakdown")
    return (f"Meter {meter_id} reports a production breakdown and is not "
            f"declared in meters.yaml")
