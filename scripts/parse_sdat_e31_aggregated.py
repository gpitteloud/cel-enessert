#!/usr/bin/env python3
"""
Parser for E31 AggregatedMeteredData_1.3 format (community aggregates)

Parses E31 XML files containing community-level aggregated energy data
and decodes them into MeteredData observations.
"""

import logging
from typing import Optional

from scripts.models import MeteredData, classify_metric_type, flow_to_direction
from scripts.sdat_xml import NS, extract_product_code, extract_resolution_minutes, parse_observations

logger = logging.getLogger(__name__)


def parse_e31(root, filename: str) -> Optional[MeteredData]:
    """
    Decode an E31 AggregatedMeteredData_1.3 document.

    Takes an already-parsed XML root element (dispatched from parse_sdat, which
    owns ET.parse and the E66/E31 document-type decision).

    Args:
        root: parsed XML root Element of an E31 document
        filename: same of currently processed SDAT file

    Returns:
        MeteredData with document_type='E31' populated, or None if the document
        has no MeteringData section.
    """
    try:
        result = MeteredData(document_type='E31', filename=filename)

        # Find MeteringData section
        metering_data = root.find('.//rsm:MeteringData', NS)
        if metering_data is None:
            logger.warning("E31: No MeteringData section found")
            return None

        # Extract interval start (base timestamp for observations)
        interval = metering_data.find('rsm:Interval', NS)
        if interval is not None:
            start_dt = interval.find('rsm:StartDateTime', NS)
            if start_dt is not None:
                result.start = start_dt.text

        # Extract resolution (missing resolution is fatal)
        resolution_minutes = extract_resolution_minutes(metering_data, NS)
        if resolution_minutes is None:
            logger.error("E31: Resolution not found")
            return None
        result.resolution_minutes = resolution_minutes

        # Extract grid area
        grid_area = metering_data.find('rsm:MeteringGridArea/rsm:EICID', NS)
        if grid_area is not None:
            result.grid_area = grid_area.text

        # Extract product code (can be ebIX or VSE)
        result.product_code, result.code_type = extract_product_code(metering_data, NS)

        # Extract aggregation criteria
        agg_criteria = metering_data.find('rsm:AggregationCriteria', NS)
        if agg_criteria is not None:
            flow = agg_criteria.find('rsm:FlowCharacteristic', NS)
            if flow is not None:
                result.flow_characteristic = flow.text

        # Extract community info
        community = metering_data.find('rsm:Community', NS)
        if community is not None:
            comm_id = community.find('rsm:CommunityID', NS)
            if comm_id is not None:
                result.community_id = comm_id.text

            comm_type = community.find('rsm:CommunityType/rsm:VSENationalCode', NS)
            if comm_type is not None:
                result.community_type = comm_type.text

        # Classify into the shared MetricType, same scheme as E66. Direction
        # comes from the flow characteristic (E17 consumption / E18 production)
        # rather than a metering point type.
        direction = flow_to_direction(result.flow_characteristic)
        result.metric_type = classify_metric_type(direction, result.product_code)

        # Parse observations (need the interval start to time-stamp them)
        if result.start is None:
            logger.error("E31: No start datetime found")
            return None

        result.observations = parse_observations(
            metering_data, NS, result.start, resolution_minutes)
        logger.info(f"E31: Parsed {len(result.observations)} community aggregate observations")
        return result

    except Exception as e:
        logger.error(f"Error decoding E31 document: {e}", exc_info=True)
        return None
