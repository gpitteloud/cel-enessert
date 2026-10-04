#!/usr/bin/env python3
"""
E66 (ValidatedMeteredData_1.6) -> MeteredData: individual meter readings.

The XML is already read into a FileHeader (see sdat_header). What is left is
the one decision E66 needs and E31 does not: whether this file's readings are
stored at all. When they are, they go under the file's own meter id, tagged
with the customer that owns it.

Every rule below is a lookup in the declared meters (see scripts/meters.py), so
one file is decidable on its own: a late or retried file needs no batch around it.
"""
import logging
from typing import Optional

from scripts.meters import Meters
from scripts.models import MeteredData, ParseResult, SkippedDocument
from scripts.sdat_header import FileHeader

logger = logging.getLogger(__name__)


def reports_against_its_role(header: FileHeader, meters: Meters) -> bool:
    """True for a file whose direction is not its declared meter's role.

    The provider sends two kinds: the copy of the production total a consumption
    point sends when its customer also produces, and the consumption files of
    production point 0134575W (see PROVIDER_QUESTIONS.md). An undeclared meter has
    no role, so nothing it sends is against it.
    """
    role = meters.role_of(header.file_meter_id)
    return role is not None and header.metering_point_type != role


def stored_elsewhere(header: FileHeader, meters: Meters) -> bool:
    """True when another meter of the same customer carries this file's direction.

    Then a file reporting against its role is a duplicate or a provider fault,
    and dropping it loses nothing.
    """
    customer = meters.customer_of(header.file_meter_id)
    return (customer is not None
            and meters.customer_has(customer, header.metering_point_type))


def parse_e66(header: FileHeader,
              meters: Optional[Meters] = None) -> ParseResult:
    """
    Turn an E66 FileHeader into the rows it should be stored as.

    Args:
        header: the parsed file, observations included
        meters: the declared meters; without them every meter is undeclared and
            is stored with no customer

    Returns:
        MeteredData with document_type='E66';
        SkippedDocument if a total file reports against its meter's role and
        the customer's other meter carries that direction (a production-total
        copy on a consumption point, or a consumption total on a production
        point);
        None if the file has no meter id, is a CEL or grid breakdown against
        its meter's role (the role is wrong), or is a total against its role
        with nothing else carrying it.
    """
    meters = meters if meters is not None else Meters.empty()

    meter_id = header.file_meter_id
    if not meter_id:
        logger.error(f"{header.file_name}: no consumption or production meter id")
        return None

    customer_id = meters.customer_of(meter_id)
    if reports_against_its_role(header, meters):
        role = meters.role_of(meter_id)
        if header.segment != 'total':
            # A meter measures only its own direction, so a CEL or grid
            # breakdown names its role. Both expected skips are total files;
            # a breakdown against the role means the declared role is wrong,
            # and skipping it would drop the meter's data silently.
            logger.error(f"{role} meter {meter_id} of customer {customer_id} "
                         f"reports a {header.metering_point_type} "
                         f"{header.segment} breakdown, which only a "
                         f"{header.metering_point_type} meter measures. Check "
                         f"its role in customers.yaml. Skipping "
                         f"{header.file_name}.")
            return None
        if stored_elsewhere(header, meters):
            # Intentional, so reported as a skip: the caller logs it as
            # expected and still archives the file.
            return SkippedDocument(
                reason=(f"{role} meter {meter_id} reports {header.direction}, "
                        f"which customer {customer_id}'s "
                        f"{header.metering_point_type} meter carries"),
                meter_id=meter_id,
            )
        # A consumption-only customer whose meter starts reporting production
        # has most likely installed solar before the list was updated: dropping
        # it would lose real production.
        logger.error(f"{role} meter {meter_id} of customer {customer_id} reports "
                     f"{header.metering_point_type}, and the customer owns no "
                     f"{header.metering_point_type} meter. Is customers.yaml "
                     f"up to date? Skipping {header.file_name}.")
        return None

    if customer_id is None and not header.rcp:
        logger.warning(f"{header.file_name}: meter {meter_id} is not declared in "
                       f"customers.yaml; stored with no customer")

    if header.metric_type is None:
        logger.warning(f"{header.file_name}: unclassified product code "
                       f"{header.product_code!r}")

    # The header row records the owner too, so a file is traceable to its
    # customer without reading its rows.
    header.customer_id = customer_id

    return MeteredData(
        document_type='E66',
        filename=header.file_name,
        observations=header.observations,
        product_code=header.product_code,
        code_type=header.code_type,
        community_id=header.community_id,
        metric_type=header.metric_type,
        meter_id=meter_id,
        customer_id=customer_id,
        rcp=header.rcp,
    )
