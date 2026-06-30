#!/usr/bin/env python3
"""
SDAT ValidatedMeteredData_1.6 XML Parser for Swiss CEL

Parses Swiss energy provider XML files following ValidatedMeteredData_1.6 schema.
Schema location: http://www.strom.ch ValidatedMeteredData_1p6.xsd
"""

import sys
import os
import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import xml.etree.ElementTree as ET
import yaml
from zoneinfo import ZoneInfo

# Import VictoriaMetrics sender
from send_to_victoriametrics import send_batch

# Configuration
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG_DIR = PROJECT_DIR / "config"
LOG_DIR = PROJECT_DIR / "logs"

# Ensure directories exist
LOG_DIR.mkdir(exist_ok=True)

# Custom formatter for CET/CEST timezone
class CETFormatter(logging.Formatter):
    """Format log timestamps in CET/CEST timezone with automatic daylight saving"""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=ZoneInfo('Europe/Zurich'))
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S %Z')

# Setup logging with CET timezone
cet_formatter = CETFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

file_handler = logging.FileHandler(LOG_DIR / "parser.log")
file_handler.setFormatter(cet_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(cet_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)

# Product code mapping (ebIXCode)
PRODUCT_CODES = {
    # Consumption codes
    'consumption_total': ['8716867000016', '735_001'],
    'consumption_grid': ['8716867000023', '735_002'],
    'consumption_local': ['8716867000030', '735_003'],

    # Production codes
    'production_total': ['8716867000047', '736_001'],
    'production_grid': ['8716867000054', '736_002'],
    'production_local': ['8716867000061', '736_003'],
}

# Reverse mapping
CODE_TO_METRIC = {}
for metric, codes in PRODUCT_CODES.items():
    for code in codes:
        CODE_TO_METRIC[code] = metric


def load_config():
    """Load configuration files"""
    with open(CONFIG_DIR / "api_config.yaml", 'r', encoding='utf-8') as f:
        api_config = yaml.safe_load(f)
    return api_config


def load_meter_mappings():
    """Load physical-to-virtual meter mappings"""
    mappings_file = PROJECT_DIR / "meter_mappings.yaml"
    if not mappings_file.exists():
        logger.warning(f"Meter mappings file not found: {mappings_file}")
        return {}

    with open(mappings_file, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)

    # Create reverse mapping: virtual_meter -> physical_meter
    reverse_map = {}
    if data and 'meter_mappings' in data:
        for physical, info in data['meter_mappings'].items():
            virtual = info['virtual_meter']
            reverse_map[virtual] = physical
            logger.debug(f"Mapping: {physical} (physical) <-> {virtual} (virtual)")

    logger.info(f"Loaded {len(reverse_map)} meter mappings")
    return reverse_map


def parse_sdat_xml(xml_file: Path, meter_mappings: dict = None) -> Dict:
    """
    Parse ValidatedMeteredData_1.6 XML file

    Special handling for CEL data structure:
    - Member's consumption: Has all breakdowns (CEL, Grid, Total)
    - Member's production: Only has Total
    - Virtual meter production VSE codes: Contains member's production breakdown

    Args:
        xml_file: Path to XML file
        user_meter_suffix: Last 8 chars of user's meter ID (default: '0217130Y', for backward compat)
        meter_mappings: Dict mapping virtual_meter_id -> physical_meter_id (optional)

    Returns dict with:
    - meter_id: str
    - community_id: str
    - metric_type: str (consumption_local, production_grid, etc)
    - start_time: datetime
    - end_time: datetime
    - resolution_minutes: int
    - observations: List[{sequence: int, value: float, timestamp: datetime}]
    - is_community_production_breakdown: bool (if this is community meter with production VSE codes)
    """
    logger.info(f"Parsing XML file: {xml_file}")

    try:
        tree = ET.parse(xml_file)
        root = tree.getroot()

        # Namespace
        ns = {'rsm': 'http://www.strom.ch'}

        # Check root element
        root_tag = root.tag.split('}')[-1] if '}' in root.tag else root.tag
        logger.info(f"Root element: {root_tag}")

        # Find MeteringData element
        metering_data = root.find('.//rsm:MeteringData', ns)
        if metering_data is None:
            logger.error("No MeteringData element found")
            return None

        result = {}

        # Extract meter ID
        meter_id = None

        # Try ConsumptionMeteringPoint
        consumption_point = metering_data.find('.//rsm:ConsumptionMeteringPoint/rsm:VSENationalID', ns)
        if consumption_point is not None:
            meter_id = consumption_point.text
            result['metering_point_type'] = 'consumption'

        # Try ProductionMeteringPoint
        if meter_id is None:
            production_point = metering_data.find('.//rsm:ProductionMeteringPoint/rsm:VSENationalID', ns)
            if production_point is not None:
                meter_id = production_point.text
                result['metering_point_type'] = 'production'

        if meter_id:
            result['meter_id'] = meter_id
            logger.info(f"Meter ID: {meter_id}")
        else:
            # Aggregated data (no meter ID)
            result['meter_id'] = None
            result['metering_point_type'] = 'aggregated'
            logger.info("Aggregated metering data (no meter ID)")

        # Extract time interval
        interval = metering_data.find('.//rsm:Interval', ns)
        if interval is not None:
            start_elem = interval.find('rsm:StartDateTime', ns)
            end_elem = interval.find('rsm:EndDateTime', ns)
            if start_elem is not None and end_elem is not None:
                result['start_time'] = start_elem.text
                result['end_time'] = end_elem.text

        # Extract resolution
        resolution_elem = metering_data.find('.//rsm:Resolution/rsm:Resolution', ns)
        unit_elem = metering_data.find('.//rsm:Resolution/rsm:Unit', ns)
        resolution_minutes = 15  # Default
        if resolution_elem is not None and unit_elem is not None:
            if unit_elem.text == 'MIN':
                resolution_minutes = int(resolution_elem.text)
        result['resolution_minutes'] = resolution_minutes

        # Extract product code - try both formats
        ebix_elem = metering_data.find('.//rsm:Product/rsm:ID/rsm:ebIXCode', ns)
        vse_elem = metering_data.find('.//rsm:Product/rsm:ID/rsm:VSENationalCode', ns)

        product_code = None
        code_type = None

        if ebix_elem is not None:
            product_code = ebix_elem.text
            code_type = 'ebIXCode'
        elif vse_elem is not None:
            product_code = vse_elem.text
            code_type = 'VSENationalCode'

        if product_code:
            # Determine metric type based on BOTH product code AND metering point type
            metric_type = CODE_TO_METRIC.get(product_code)

            # Special handling for VSE codes (Swiss national codes)
            if code_type == 'VSENationalCode':
                metering_type = result.get('metering_point_type')

                if product_code == '2404050010123':
                    # CEL local exchange
                    if metering_type == 'consumption':
                        metric_type = 'consumption_local'
                    elif metering_type == 'production':
                        metric_type = 'production_local'

                elif product_code == '2404050010124':
                    # Grid (residual)
                    if metering_type == 'consumption':
                        metric_type = 'consumption_grid'
                    elif metering_type == 'production':
                        metric_type = 'production_grid'

            # Handle ebIXCode for total
            elif product_code == '8716867000030':
                metering_type = result.get('metering_point_type')
                if metering_type == 'consumption':
                    metric_type = 'consumption_total'
                elif metering_type == 'production':
                    metric_type = 'production_total'

            # Mark if this is virtual meter with production VSE codes
            # (contains member's production breakdown)
            # Check using mappings if provided, otherwise fall back to old logic
            is_virtual_production = False
            attributed_physical_meter = None

            if result.get('metering_point_type') == 'production' and code_type == 'VSENationalCode':
                meter_suffix = meter_id[-8:] if meter_id and len(meter_id) >= 8 else None

                if meter_mappings and meter_suffix in meter_mappings:
                    # Using mappings: check if this is a known virtual meter
                    is_virtual_production = True
                    attributed_physical_meter = meter_mappings[meter_suffix]
                    logger.info(f"Virtual meter {meter_suffix} -> attributing to physical meter {attributed_physical_meter}")
                elif meter_id and user_meter_suffix not in meter_id:
                    # Backward compatibility: assume it's virtual if not user's meter
                    is_virtual_production = True
                    logger.info(f"Virtual meter detected (backward compat mode)")

            result['is_community_production_breakdown'] = is_virtual_production
            result['attributed_physical_meter'] = attributed_physical_meter

            result['product_code'] = product_code
            result['code_type'] = code_type
            result['metric_type'] = metric_type

            if is_virtual_production:
                logger.info(f"Product code ({code_type}): {product_code}, Community production breakdown -> {metric_type}")
            else:
                logger.info(f"Product code ({code_type}): {product_code}, Metering point: {result.get('metering_point_type')} -> {metric_type}")
        else:
            logger.warning("No product code found")

        # Extract community info
        community_elem = metering_data.find('.//rsm:Community/rsm:CommunityID', ns)
        if community_elem is not None:
            result['community_id'] = community_elem.text

        # Extract observations
        observations = []
        for obs in metering_data.findall('.//rsm:Observation', ns):
            seq_elem = obs.find('.//rsm:Position/rsm:Sequence', ns)
            vol_elem = obs.find('.//rsm:Volume', ns)

            if seq_elem is not None and vol_elem is not None:
                try:
                    sequence = int(seq_elem.text)
                    volume = float(vol_elem.text)

                    # Calculate timestamp from start time and sequence
                    if 'start_time' in result:
                        base_dt = datetime.fromisoformat(result['start_time'].replace('Z', '+00:00'))
                        obs_dt = base_dt + timedelta(minutes=(sequence - 1) * resolution_minutes)

                        observations.append({
                            'sequence': sequence,
                            'value': volume,
                            'timestamp': obs_dt.isoformat()
                        })
                except (ValueError, TypeError) as e:
                    logger.warning(f"Error parsing observation: {e}")
                    continue

        result['observations'] = observations
        logger.info(f"Parsed {len(observations)} observations")

        return result

    except ET.ParseError as e:
        logger.error(f"XML parsing error: {e}")
        raise
    except Exception as e:
        logger.error(f"Error parsing XML: {e}")
        raise


def transform_to_datapoints(parsed_data: Dict, user_meter_id: str = None) -> List[Dict]:
    """
    Transform parsed data into VictoriaMetrics data points

    Returns VictoriaMetrics format:
    {
        "metric": {"__name__": "...", "label": "value"},
        "values": [value],
        "timestamps": [timestamp_ms]
    }

    Special handling:
    - If this is community meter production VSE codes, attribute to user meter
    """
    if not parsed_data or not parsed_data.get('observations'):
        return []

    # Metric type to metric name mapping
    METRIC_NAMES = {
        'consumption_grid': 'cel_energy_grid_import_kwh',
        'production_grid': 'cel_energy_grid_export_kwh',
        'consumption_local': 'cel_energy_local_import_kwh',
        'production_local': 'cel_energy_local_export_kwh',
        'consumption_total': 'cel_energy_consumed_kwh',
        'production_total': 'cel_energy_produced_kwh',
    }

    data_points = []
    meter_id = parsed_data.get('meter_id')
    metric_type = parsed_data.get('metric_type')
    product_code = parsed_data.get('product_code', 'unknown')
    code_type = parsed_data.get('code_type', 'unknown')
    is_community_breakdown = parsed_data.get('is_community_production_breakdown', False)

    if not metric_type:
        logger.warning("No metric type found, skipping")
        return []

    # Get metric name
    metric_name = METRIC_NAMES.get(metric_type)
    if not metric_name:
        logger.warning(f"Unknown metric type: {metric_type}")
        return []

    # If this is community meter with production VSE codes, use user's meter ID
    if is_community_breakdown and user_meter_id:
        meter_id = user_meter_id
        logger.info(f"Community production breakdown attributed to user meter: {user_meter_id}")

    # Convert each observation to VictoriaMetrics format
    for obs in parsed_data['observations']:
        timestamp_dt = datetime.fromisoformat(obs['timestamp'].replace('Z', '+00:00'))
        timestamp_ms = int(timestamp_dt.timestamp() * 1000)

        # Build labels
        labels = {
            '__name__': metric_name,
            'project': 'cel',
            'product_code': product_code,
            'code_type': code_type,
            'data_type': 'consumption' if 'consumption' in metric_type else 'production',
        }

        if meter_id:
            labels['meter_id'] = meter_id

        data_point = {
            'metric': labels,
            'values': [obs['value']],
            'timestamps': [timestamp_ms]
        }

        data_points.append(data_point)

    return data_points


def send_to_victoriametrics_wrapper(data_points: List[Dict], api_config: Dict, dry_run: bool = False):
    """Send data points to VictoriaMetrics"""

    vm_url = api_config['victoriametrics']['url']
    batch_size = api_config['processing'].get('batch_size', 1000)

    logger.info(f"Sending {len(data_points)} data points to VictoriaMetrics")

    if dry_run:
        logger.info("DRY RUN - Would send:")
        for point in data_points[:5]:  # Show first 5
            logger.info(f"  {point}")
        if len(data_points) > 5:
            logger.info(f"  ... and {len(data_points) - 5} more")
        return

    # Send to VictoriaMetrics
    success_count, error_count = send_batch(data_points, vm_url, batch_size)

    if error_count > 0:
        logger.warning(f"Failed to send {error_count} data points")
    else:
        logger.info("All data sent successfully")


def main():
    parser = argparse.ArgumentParser(description='Parse ValidatedMeteredData_1.6 XML files')
    parser.add_argument('xml_file', help='Path to XML file')
    parser.add_argument('--dry-run', action='store_true', help='Parse but don\'t send to VictoriaMetrics')
    parser.add_argument('--validate-only', action='store_true', help='Only validate XML structure')
    args = parser.parse_args()

    xml_file = Path(args.xml_file)
    if not xml_file.exists():
        logger.error(f"File not found: {xml_file}")
        sys.exit(1)

    # Load configuration
    api_config = load_config()
    user_meter_suffix = api_config.get('project', {}).get('user_meter_suffix', '0217130Y')

    # Parse XML
    try:
        parsed_data = parse_sdat_xml(xml_file, user_meter_suffix=user_meter_suffix)
    except Exception as e:
        logger.error(f"Failed to parse XML: {e}")
        sys.exit(1)

    if not parsed_data:
        logger.error("No data parsed from XML")
        sys.exit(1)

    if args.validate_only:
        logger.info("XML validation successful")
        logger.info(f"  Meter ID: {parsed_data.get('meter_id', 'N/A')}")
        logger.info(f"  Metric type: {parsed_data.get('metric_type', 'N/A')}")
        logger.info(f"  Observations: {len(parsed_data.get('observations', []))}")
        logger.info(f"  Is community breakdown: {parsed_data.get('is_community_production_breakdown', False)}")
        sys.exit(0)

    # Get full user meter ID from parsed data (for community breakdown attribution)
    user_full_meter_id = None
    if parsed_data.get('is_community_production_breakdown'):
        # Need to provide the full meter ID for attribution
        # Extract from config or build from suffix
        user_full_meter_id = f"CH101110123450000000000000{user_meter_suffix}"

    # Transform to data points
    data_points = transform_to_datapoints(parsed_data, user_meter_id=user_full_meter_id)

    if not data_points:
        logger.warning("No data points generated")
        sys.exit(0)

    # Send to VictoriaMetrics
    send_to_victoriametrics_wrapper(data_points, api_config, dry_run=args.dry_run)

    logger.info("Processing complete")


if __name__ == '__main__':
    main()
