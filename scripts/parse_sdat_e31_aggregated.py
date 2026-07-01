#!/usr/bin/env python3
"""
Parser for E31 AggregatedMeteredData_1.3 format (community aggregates)

Parses E31 XML files containing community-level aggregated energy data
and converts to VictoriaMetrics format.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)


def parse_e31_xml(xml_file: Path) -> Dict:
    """
    Parse E31 AggregatedMeteredData_1.3 XML file

    Args:
        xml_file: Path to E31 XML file

    Returns:
        Dict with parsed data including observations and metadata
    """
    try:
        tree = ET.parse(xml_file)
        root = tree.getroot()

        # Verify this is E31
        doc_type = root.find('.//{http://www.strom.ch}DocumentType/{http://www.strom.ch}ebIXCode')
        if doc_type is None or doc_type.text != 'E31':
            logger.warning(f"{xml_file.name}: Not an E31 file (DocumentType={doc_type.text if doc_type is not None else 'None'})")
            return {}

        result = {
            'file_name': xml_file.name,
            'document_type': 'E31',
            'observations': []
        }

        # Extract metadata from header
        biz_reason = root.find('.//{http://www.strom.ch}BusinessReasonType/{http://www.strom.ch}VSENationalCode')
        if biz_reason is not None:
            result['business_reason'] = biz_reason.text

        # Find MeteringData section
        metering_data = root.find('.//{http://www.strom.ch}MeteringData')
        if metering_data is None:
            logger.warning(f"{xml_file.name}: No MeteringData section found")
            return {}

        # Extract interval (data period)
        interval = metering_data.find('{http://www.strom.ch}Interval')
        if interval is not None:
            start_dt = interval.find('{http://www.strom.ch}StartDateTime')
            end_dt = interval.find('{http://www.strom.ch}EndDateTime')
            if start_dt is not None and end_dt is not None:
                result['start_datetime'] = start_dt.text
                result['end_datetime'] = end_dt.text

        # Extract resolution
        resolution_elem = metering_data.find('{http://www.strom.ch}Resolution')
        if resolution_elem is not None:
            res_value = resolution_elem.find('{http://www.strom.ch}Resolution')
            res_unit = resolution_elem.find('{http://www.strom.ch}Unit')
            if res_value is not None and res_unit is not None:
                result['resolution'] = int(res_value.text)
                result['resolution_unit'] = res_unit.text

        # Extract grid area
        grid_area = metering_data.find('{http://www.strom.ch}MeteringGridArea/{http://www.strom.ch}EICID')
        if grid_area is not None:
            result['grid_area'] = grid_area.text

        # Extract product code (can be ebIX or VSE)
        product = metering_data.find('{http://www.strom.ch}Product')
        if product is not None:
            # Try ebIX code first
            product_id = product.find('{http://www.strom.ch}ID/{http://www.strom.ch}ebIXCode')
            if product_id is not None:
                result['product_code'] = product_id.text
                result['product_code_type'] = 'ebIX'
            else:
                # Try VSE code
                product_id = product.find('{http://www.strom.ch}ID/{http://www.strom.ch}VSENationalCode')
                if product_id is not None:
                    result['product_code'] = product_id.text
                    result['product_code_type'] = 'VSE'

            measure_unit = product.find('{http://www.strom.ch}MeasureUnit')
            if measure_unit is not None:
                result['measure_unit'] = measure_unit.text

        # Extract aggregation criteria
        agg_criteria = metering_data.find('{http://www.strom.ch}AggregationCriteria')
        if agg_criteria is not None:
            flow = agg_criteria.find('{http://www.strom.ch}FlowCharacteristic')
            if flow is not None:
                result['flow_characteristic'] = flow.text

            settlement = agg_criteria.find('{http://www.strom.ch}SettlementMethodCharacteristic')
            if settlement is not None:
                result['settlement_method'] = settlement.text

        # Extract community info
        community = metering_data.find('{http://www.strom.ch}Community')
        if community is not None:
            comm_id = community.find('{http://www.strom.ch}CommunityID')
            if comm_id is not None:
                result['community_id'] = comm_id.text

            comm_type = community.find('{http://www.strom.ch}CommunityType/{http://www.strom.ch}VSENationalCode')
            if comm_type is not None:
                result['community_type'] = comm_type.text

        # Parse observations
        observations = metering_data.findall('{http://www.strom.ch}Observation')

        if not observations:
            logger.warning(f"{xml_file.name}: No observations found")
            return result

        # Base timestamp from start_datetime
        if 'start_datetime' not in result:
            logger.error(f"{xml_file.name}: No start_datetime found")
            return result

        base_time = datetime.fromisoformat(result['start_datetime'].replace('Z', '+00:00'))
        resolution_minutes = result.get('resolution', 15)

        for obs in observations:
            position = obs.find('{http://www.strom.ch}Position')
            if position is None:
                continue

            sequence = position.find('{http://www.strom.ch}Sequence')
            if sequence is None:
                continue

            seq_num = int(sequence.text)

            volume = obs.find('{http://www.strom.ch}Volume')
            if volume is None:
                continue

            # Calculate timestamp for this observation
            timestamp = base_time + timedelta(minutes=(seq_num - 1) * resolution_minutes)

            # Get condition flag if present
            condition = obs.find('{http://www.strom.ch}Condition')
            condition_code = condition.text if condition is not None else None

            result['observations'].append({
                'sequence': seq_num,
                'timestamp': timestamp.isoformat(),
                'value': float(volume.text),
                'condition': condition_code
            })

        logger.info(f"{xml_file.name}: Parsed {len(result['observations'])} community aggregate observations")
        return result

    except Exception as e:
        logger.error(f"Error parsing {xml_file}: {e}", exc_info=True)
        return {}


def transform_e31_to_datapoints(parsed_data: Dict) -> List[Dict]:
    """
    Transform parsed E31 data to VictoriaMetrics data points

    Args:
        parsed_data: Output from parse_e31_xml()

    Returns:
        List of data points in VictoriaMetrics NDJSON format
    """
    if not parsed_data or not parsed_data.get('observations'):
        return []

    data_points = []

    # Extract metadata for labels
    community_id = parsed_data.get('community_id', 'unknown')
    community_type = parsed_data.get('community_type', 'unknown')
    product_code = parsed_data.get('product_code', 'unknown')
    flow = parsed_data.get('flow_characteristic', 'unknown')
    grid_area = parsed_data.get('grid_area', 'unknown')

    # Create metric name
    metric_name = 'energy_community_aggregate_kwh'

    for obs in parsed_data['observations']:
        timestamp_dt = datetime.fromisoformat(obs['timestamp'])
        timestamp_ms = int(timestamp_dt.timestamp() * 1000)

        # Build labels
        labels = {
            'community_id': community_id,
            'community_type': community_type,
            'product_code': product_code,
            'flow_characteristic': flow,
            'grid_area': grid_area,
            'data_source': 'E31_AggregatedMeteredData',
        }

        # Add condition if present
        if obs.get('condition'):
            labels['condition'] = obs['condition']

        data_point = {
            'metric': {
                '__name__': metric_name,
                **labels
            },
            'values': [obs['value']],
            'timestamps': [timestamp_ms]
        }

        data_points.append(data_point)

    return data_points
