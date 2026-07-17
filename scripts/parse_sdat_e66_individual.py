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
import xml.etree.ElementTree as ET

from models import MetricType, Observation, MeteredData

logger = logging.getLogger(__name__)


def parse_e66(root, meter_mappings: dict = None, physical_production_meters: set = None) -> Optional[MeteredData]:
    """
    Decode a ValidatedMeteredData_1.6 (E66) document.

    Takes an already-parsed XML root element (dispatched from parse_sdat, which
    owns ET.parse and the E66/E31 document-type decision).

    Special handling for CEL data structure:
    - Member's consumption: Has all breakdowns (CEL, Grid, Total)
    - Member's production: Only has Total
    - Virtual meter production VSE codes: Contains member's production breakdown

    Args:
        root: parsed XML root Element of an E66 document
        meter_mappings: Dict mapping virtual_meter_id -> physical_meter_id (optional)
        physical_production_meters: Set of meter suffixes that report an ebIX
            production total. Used to detect self-contained meters that carry
            both the total and the VSE breakdown on the same meter ID (optional)

    Returns:
        MeteredData with document_type='E66' populated, or None if the document
        lacks required content (missing MeteringData, meter_id, or resolution).
    """
    try:
        # Namespace
        ns = {'rsm': 'http://www.strom.ch'}

        # Find MeteringData element
        metering_data = root.find('.//rsm:MeteringData', ns)
        if metering_data is None:
            logger.error("No MeteringData element found")
            return None

        result = MeteredData(document_type='E66')

        # Extract meter ID
        meter_id = None

        # Try ConsumptionMeteringPoint
        consumption_point = metering_data.find('.//rsm:ConsumptionMeteringPoint/rsm:VSENationalID', ns)
        if consumption_point is not None:
            meter_id = consumption_point.text
            result.metering_point_type = 'consumption'

        # Try ProductionMeteringPoint
        if meter_id is None:
            production_point = metering_data.find('.//rsm:ProductionMeteringPoint/rsm:VSENationalID', ns)
            if production_point is not None:
                meter_id = production_point.text
                result.metering_point_type = 'production'

        if meter_id:
            result.meter_id = meter_id
            logger.info(f"Meter ID: {meter_id}")
        else:
            logger.error("meter_id not found in consumption and production")
            return None

        # Extract time interval
        interval = metering_data.find('.//rsm:Interval', ns)
        if interval is not None:
            start_elem = interval.find('rsm:StartDateTime', ns)
            end_elem = interval.find('rsm:EndDateTime', ns)
            if start_elem is not None and end_elem is not None:
                result.start = start_elem.text
                result.end = end_elem.text

        # Extract resolution
        resolution_elem = metering_data.find('.//rsm:Resolution/rsm:Resolution', ns)
        unit_elem = metering_data.find('.//rsm:Resolution/rsm:Unit', ns)
        resolution_minutes = None
        if resolution_elem is not None and unit_elem is not None:
            if unit_elem.text == 'MIN':
                resolution_minutes = int(resolution_elem.text)
        if resolution_minutes is None:
            logger.error("Resolution not found")
            return None
        result.resolution_minutes = resolution_minutes

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
            metric_type = determine_metric_type(product_code, result)

            # Mark if this is a production breakdown file (VSE CEL/Grid codes on
            # a production point). Attribute it to a physical meter, either a
            # mapped separate virtual meter or a self-contained meter (itself).
            is_production_breakdown = False
            attributed_physical_meter = None

            if result.metering_point_type == 'production' and code_type == 'VSENationalCode':
                meter_suffix = meter_id[-8:] if meter_id and len(meter_id) >= 8 else None

                if meter_mappings and meter_suffix in meter_mappings:
                    # Using mappings: this is a separate virtual meter mapped to a physical one
                    is_production_breakdown = True
                    attributed_physical_meter = meter_mappings[meter_suffix]
                    logger.info(f"Virtual meter {meter_suffix} -> attributing to physical meter {attributed_physical_meter}")
                elif meter_suffix and physical_production_meters and meter_suffix in physical_production_meters:
                    # Self-contained meter: the production breakdown is on the same meter ID
                    # that also reports the ebIX production total. Attribute to itself.
                    is_production_breakdown = True
                    attributed_physical_meter = meter_suffix
                    logger.info(f"Self-contained meter {meter_suffix} -> attributing production breakdown to itself")
                else:
                    # No mapping found - skip this virtual meter
                    logger.error(f"Unknown virtual meter {meter_suffix} - no mapping found in auto-discovery. Skipping file.")
                    return None

            result.is_production_breakdown = is_production_breakdown
            result.attributed_physical_meter = attributed_physical_meter

            result.product_code = product_code
            result.code_type = code_type
            result.metric_type = metric_type

            if is_production_breakdown:
                logger.info(f"Product code ({code_type}): {product_code}, production breakdown -> {metric_type}")
            else:
                logger.info(f"Product code ({code_type}): {product_code}, Metering point: {result.metering_point_type} -> {metric_type}")
        else:
            logger.warning("No product code found")

        # Extract community info
        community_elem = metering_data.find('.//rsm:Community/rsm:CommunityID', ns)
        if community_elem is not None:
            result.community_id = community_elem.text

        # Extract observations
        observations = []
        # ~480 observations/file, ~103 files/day, parsed in <1s total.
        # Sequential is fast enough; no need for pandas/parallelism.
        for obs in metering_data.findall('.//rsm:Observation', ns):
            seq_elem = obs.find('.//rsm:Position/rsm:Sequence', ns)
            vol_elem = obs.find('.//rsm:Volume', ns)

            if seq_elem is not None and vol_elem is not None:
                try:
                    sequence = int(seq_elem.text)
                    volume = float(vol_elem.text)

                    # Calculate timestamp from start time and sequence
                    if result.start:
                        base_dt = datetime.fromisoformat(result.start.replace('Z', '+00:00'))
                        obs_dt = base_dt + timedelta(minutes=(sequence - 1) * resolution_minutes)

                        observations.append(Observation(
                            sequence=sequence,
                            value=volume,
                            timestamp=obs_dt.isoformat(),
                        ))
                except (ValueError, TypeError) as e:
                    logger.warning(f"Error parsing observation: {e}")
                    continue

        result.observations = observations
        logger.info(f"Parsed {len(observations)} observations")

        return result

    except Exception as e:
        logger.error(f"Error decoding E66 document: {e}", exc_info=True)
        return None


def determine_metric_type(product_code: str, result: MeteredData) -> Optional[MetricType]:
    """Map a product code + metering point type to a MetricType.

    Returns None if the (product_code, metering_point_type) combination is not
    one of the recognized ones.
    """
    metric_type = None
    metering_type = result.metering_point_type
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

    elif product_code == '8716867000030':
        # Total (Grid + local)
        if metering_type == 'consumption':
            metric_type = MetricType.CONSUMPTION_TOTAL
        elif metering_type == 'production':
            metric_type = MetricType.PRODUCTION_TOTAL
    return metric_type


def transform_to_datapoints(parsed_data: Optional[MeteredData], attributed_meter_id: str = None) -> List[Dict]:
    """
    Transform parsed data into VictoriaMetrics data points

    Returns VictoriaMetrics format:
    {
        "metric": {"__name__": "...", "label": "value"},
        "values": [value],
        "timestamps": [timestamp_ms]
    }

    Args:
        parsed_data: MeteredData from parse_e66()
        attributed_meter_id: for a production breakdown file, the full ID of the
            physical meter the breakdown belongs to (from mapping or self-
            contained detection). Overrides the meter_id label so the breakdown
            is stored against the physical meter rather than the virtual one.
    """
    if not parsed_data or not parsed_data.observations:
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
    meter_id = parsed_data.meter_id
    metric_type = parsed_data.metric_type
    product_code = parsed_data.product_code or 'unknown'
    code_type = parsed_data.code_type or 'unknown'
    is_production_breakdown = parsed_data.is_production_breakdown

    if not metric_type:
        logger.warning("No metric type found, skipping")
        return []

    # Get metric name
    metric_name = METRIC_NAMES.get(metric_type)
    if not metric_name:
        logger.warning(f"Unknown metric type: {metric_type}")
        return []

    # For a production breakdown file, store it against the attributed physical
    # meter (from mapping or self-contained detection) rather than the virtual
    # meter ID that appears in the file itself.
    if is_production_breakdown and attributed_meter_id:
        meter_id = attributed_meter_id
        logger.info(f"Production breakdown attributed to physical meter: {attributed_meter_id}")

    # Convert each observation to VictoriaMetrics format
    for obs in parsed_data.observations:
        timestamp_dt = datetime.fromisoformat(obs.timestamp.replace('Z', '+00:00'))
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

        #TG: Isn't this unnecessarily cluttering the database with duplicate labels?
        #  GP: NO, labels are not duplicated when stored in VM, it's the other way around:
        #      1 labels set used to identify the series, then N samples appended to it.
        # Would it be possible for each datapoint to refer to a separate labels table, which would include the document name?
        #  GP: including document name in the labels would fragment each meter time series extraction by delivery
        #      -> querying over a month would complexify and slow down queries.
        data_point = {
            'metric': labels,
            'values': [obs.value],
            'timestamps': [timestamp_ms]
        }

        data_points.append(data_point)

    return data_points


