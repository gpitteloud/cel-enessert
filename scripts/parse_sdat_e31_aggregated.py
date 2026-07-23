#!/usr/bin/env python3
"""
Parser for E31 AggregatedMeteredData_1.3 format (community aggregates)

Parses E31 XML files containing community-level aggregated energy data
and converts to VictoriaMetrics format.
"""

from datetime import datetime
from typing import Dict, List, Optional
import logging

from models import MeteredData, classify_metric_type, flow_to_direction
from sdat_xml import extract_product_code, extract_resolution_minutes, parse_observations

logger = logging.getLogger(__name__)


def parse_e31(root) -> Optional[MeteredData]:
    """
    Decode an E31 AggregatedMeteredData_1.3 document.

    Takes an already-parsed XML root element (dispatched from parse_sdat, which
    owns ET.parse and the E66/E31 document-type decision).

    Args:
        root: parsed XML root Element of an E31 document

    Returns:
        MeteredData with document_type='E31' populated, or None if the document
        has no MeteringData section.
    """
    try:
        # Namespace
        ns = {'rsm': 'http://www.strom.ch'}

        result = MeteredData(document_type='E31')

        # Find MeteringData section
        metering_data = root.find('.//rsm:MeteringData', ns)
        if metering_data is None:
            logger.warning("E31: No MeteringData section found")
            return None

        # Extract interval start (base timestamp for observations)
        interval = metering_data.find('rsm:Interval', ns)
        if interval is not None:
            start_dt = interval.find('rsm:StartDateTime', ns)
            if start_dt is not None:
                result.start = start_dt.text

        # Extract resolution (missing resolution is fatal)
        resolution_minutes = extract_resolution_minutes(metering_data, ns)
        if resolution_minutes is None:
            logger.error("E31: Resolution not found")
            return None
        result.resolution_minutes = resolution_minutes

        # Extract grid area
        grid_area = metering_data.find('rsm:MeteringGridArea/rsm:EICID', ns)
        if grid_area is not None:
            result.grid_area = grid_area.text

        # Extract product code (can be ebIX or VSE)
        result.product_code, result.code_type = extract_product_code(metering_data, ns)

        # Extract aggregation criteria
        agg_criteria = metering_data.find('rsm:AggregationCriteria', ns)
        if agg_criteria is not None:
            flow = agg_criteria.find('rsm:FlowCharacteristic', ns)
            if flow is not None:
                result.flow_characteristic = flow.text

        # Extract community info
        community = metering_data.find('rsm:Community', ns)
        if community is not None:
            comm_id = community.find('rsm:CommunityID', ns)
            if comm_id is not None:
                result.community_id = comm_id.text

            comm_type = community.find('rsm:CommunityType/rsm:VSENationalCode', ns)
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
            metering_data, ns, result.start, resolution_minutes)
        logger.info(f"E31: Parsed {len(result.observations)} community aggregate observations")
        return result

    except Exception as e:
        logger.error(f"Error decoding E31 document: {e}", exc_info=True)
        return None


def transform_e31_to_datapoints(parsed_data: Optional[MeteredData]) -> List[Dict]:
    """
    Transform parsed E31 data to VictoriaMetrics data points

    Args:
        parsed_data: MeteredData from parse_e31()

    Returns:
        List of data points in VictoriaMetrics NDJSON format
    """
    if not parsed_data or not parsed_data.observations:
        return []

    data_points = []

    # Extract metadata for labels
    community_id = parsed_data.community_id or 'unknown'
    community_type = parsed_data.community_type or 'unknown'
    product_code = parsed_data.product_code or 'unknown'
    code_type = parsed_data.code_type or 'unknown'
    grid_area = parsed_data.grid_area or 'unknown'
    metric_type = parsed_data.metric_type

    # Community aggregate (E31) kept under its own metric name so it never mixes
    # with the per-meter E66 series (which would double-count on sum()). The two
    # shared dimensions are exposed as labels, same scheme as E66:
    #   direction = consumption | production
    #   segment   = cel | grid | total
    metric_name = 'cel_community_energy_kwh'

    for obs in parsed_data.observations:
        timestamp_dt = datetime.fromisoformat(obs.timestamp)
        timestamp_ms = int(timestamp_dt.timestamp() * 1000)

        # Build labels
        labels = {
            'project': 'cel',
            'community_id': community_id,
            'community_type': community_type,
            'product_code': product_code,
            'code_type': code_type,
            'grid_area': grid_area,
        }

        # Shared direction/segment labels (only when the metric type classified;
        # e.g. an unknown flow leaves them off rather than emitting 'unknown').
        if metric_type:
            labels['direction'] = metric_type.direction
            labels['segment'] = metric_type.segment

        # Add condition if present
        if obs.condition:
            labels['condition'] = obs.condition

        data_point = {
            'metric': {
                '__name__': metric_name,
                **labels
            },
            'values': [obs.value],
            'timestamps': [timestamp_ms]
        }

        data_points.append(data_point)

    return data_points
