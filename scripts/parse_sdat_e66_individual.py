#!/usr/bin/env python3
"""
SDAT ValidatedMeteredData_1.6 XML Parser for Swiss CEL

Parses Swiss energy provider XML files following ValidatedMeteredData_1.6 schema.
Schema location: http://www.strom.ch ValidatedMeteredData_1p6.xsd
"""
# TG: pour plus de robustesse, je proposerais de ne pas splitter en 2 scripts lancés en fonction du nom de fichier, 
# mais plutôt de vérifier la valeur de rsm:ValidatedMeteredData_HeaderInformation/rsm:InstanceDocument/rsm:DocumentType au moment de la lecture du fichier 

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from enum import Enum
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


class MetricType(str, Enum):
    """Energy metric types"""
    CONSUMPTION_TOTAL = 'consumption_total'
    CONSUMPTION_GRID = 'consumption_grid'
    CONSUMPTION_LOCAL = 'consumption_local'
    PRODUCTION_TOTAL = 'production_total'
    PRODUCTION_GRID = 'production_grid'
    PRODUCTION_LOCAL = 'production_local'


# Product code mapping (ebIXCode)
PRODUCT_CODES = {
    # Consumption codes
    # TG: Comment sont déterminés ces mapping ? d'après les guidelines de l'AES, Core Components, Annexe 3, les codes correspondent à ce qui suit:
    MetricType.CONSUMPTION_TOTAL: ['8716867000016', '735_001'],
    # TG -> Puissance active
    MetricType.CONSUMPTION_GRID: ['8716867000023', '735_002'],
    # TG -> Puissance réactive
    MetricType.CONSUMPTION_LOCAL: ['8716867000030', '735_003'],
    # TG -> Énergie active

    # Production codes
    MetricType.PRODUCTION_TOTAL: ['8716867000047', '736_001'],
    # TG -> Énergie réactive
    MetricType.PRODUCTION_GRID: ['8716867000054', '736_002'],
    # TG -> ? non mentionné
    MetricType.PRODUCTION_LOCAL: ['8716867000061', '736_003'],
    # TG -> ? non mentionné
}

# Reverse mapping: product code -> MetricType
CODE_TO_METRIC: Dict[str, MetricType] = {}
for metric, codes in PRODUCT_CODES.items():
    for code in codes:
        CODE_TO_METRIC[code] = metric


def parse_sdat_xml(xml_file: Path, meter_mappings: dict = None) -> Dict:
    """
    Parse ValidatedMeteredData_1.6 XML file

    Special handling for CEL data structure:
    - Member's consumption: Has all breakdowns (CEL, Grid, Total)
    - Member's production: Only has Total
    - Virtual meter production VSE codes: Contains member's production breakdown

    Args:
        xml_file: Path to XML file
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
            # TG ce cas de figure n'est pas possible pour des données individuelles, il faut lancer une erreur si on tombe dessus.  
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
        # TG: je ne mettrais pas de valeur par défaut ici - il faut renvoyer une erreur si la résolution n'est pas fournie
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
            # TG: On n'a pas besoin de vérifier le code_type: les codes de type 2404050010123/2404050010124 sont nécessairement VSENAtionalCode
            if code_type == 'VSENationalCode':
                metering_type = result.get('metering_point_type')

                if product_code == '2404050010123':
                    # CEL local exchange
                    if metering_type == 'consumption':
                        metric_type = MetricType.CONSUMPTION_LOCAL
                    elif metering_type == 'production':
                        metric_type = MetricType.PRODUCTION_LOCAL

                elif product_code == '2404050010124':
                    # Grid (residual)
                    if metering_type == 'consumption':
                        metric_type = MetricType.CONSUMPTION_GRID
                    elif metering_type == 'production':
                        metric_type = MetricType.PRODUCTION_GRID

            # Handle ebIXCode for total
            elif product_code == '8716867000030':
                metering_type = result.get('metering_point_type')
                if metering_type == 'consumption':
                    metric_type = MetricType.CONSUMPTION_TOTAL
                elif metering_type == 'production':
                    metric_type = MetricType.PRODUCTION_TOTAL

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
                else:
                    # No mapping found - skip this virtual meter
                    logger.error(f"Unknown virtual meter {meter_suffix} - no mapping found in auto-discovery. Skipping file.")
                    return None

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
        # TG faire ceci séquenciellement n'est-il pas très long? Potentiellement plus efficace de paralelliser les traitements (utiliser pandas? 
        # https://pandas.pydata.org/docs/reference/api/pandas.read_xml.html )
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
        MetricType.CONSUMPTION_GRID: 'cel_energy_grid_import_kwh',
        MetricType.PRODUCTION_GRID: 'cel_energy_grid_export_kwh',
        # TG: c'est quoi cel_energy_local_import_kwh et cel_energy_local_export_kwh ?
        MetricType.CONSUMPTION_LOCAL: 'cel_energy_local_import_kwh',
        MetricType.PRODUCTION_LOCAL: 'cel_energy_local_export_kwh',
        MetricType.CONSUMPTION_TOTAL: 'cel_energy_consumed_kwh',
        MetricType.PRODUCTION_TOTAL: 'cel_energy_produced_kwh',
    }

    data_points = []
    meter_id = parsed_data.get('meter_id')
    metric_type: Optional[MetricType] = parsed_data.get('metric_type')
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
    # TG: What is meant with "community meter" ? Why fall back to using the meter of the user ? the xml provides a community ID
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
            'data_type': 'consumption' if 'consumption' in metric_type.value else 'production',
        }

        if meter_id:
            labels['meter_id'] = meter_id

        #TG: Isn't this unnecesarily clutering the database with duplicate labels? 
        # Would it be possible for each datapoint to refer to a separate labels table, which would include the document name?
        data_point = {
            'metric': labels,
            'values': [obs['value']],
            'timestamps': [timestamp_ms]
        }

        data_points.append(data_point)

    return data_points


