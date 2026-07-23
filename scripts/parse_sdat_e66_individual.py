#!/usr/bin/env python3
"""
SDAT ValidatedMeteredData_1.6 XML Parser for Swiss CEL

Parses Swiss energy provider XML files following ValidatedMeteredData_1.6 schema.
Schema location: http://www.strom.ch ValidatedMeteredData_1p6.xsd
"""
# TG: pour plus de robustesse, je proposerais de ne pas splitter en 2 scripts lancés en fonction du nom de fichier, 
# mais plutôt de vérifier la valeur de rsm:ValidatedMeteredData_HeaderInformation/rsm:InstanceDocument/rsm:DocumentType au moment de la lecture du fichier 

import logging
from datetime import datetime
from typing import Dict, List, Optional

from models import MetricType, MeteredData, classify_metric_type
from sdat_xml import extract_product_code, extract_resolution_minutes, parse_observations

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

        # Extract interval start (base timestamp for observations)
        interval = metering_data.find('.//rsm:Interval', ns)
        if interval is not None:
            start_elem = interval.find('rsm:StartDateTime', ns)
            if start_elem is not None:
                result.start = start_elem.text

        # Extract resolution (missing resolution is fatal)
        resolution_minutes = extract_resolution_minutes(metering_data, ns)
        if resolution_minutes is None:
            logger.error("Resolution not found")
            return None
        result.resolution_minutes = resolution_minutes

        # Extract product code - try both formats
        product_code, code_type = extract_product_code(metering_data, ns)

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
                    # As of 2026-07, meter 0134575W (not linked to RCP) is the only
                    # such meter -- the sole breakdown attributed to itself rather
                    # than to a separate 085-prefixed virtual meter.
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

        # Extract observations (need the interval start to time-stamp them)
        if result.start is None:
            logger.error("No start datetime found")
            return None

        # ~480 observations/file, ~103 files/day, parsed in <1s total.
        # Sequential is fast enough; no need for pandas/parallelism.
        result.observations = parse_observations(
            metering_data, ns, result.start, resolution_minutes)
        logger.info(f"Parsed {len(result.observations)} observations")

        return result

    except Exception as e:
        logger.error(f"Error decoding E66 document: {e}", exc_info=True)
        return None


def determine_metric_type(product_code: str, result: MeteredData) -> Optional[MetricType]:
    """Map a product code + metering point type to a MetricType.

    The E66 metering point type ('consumption'|'production') is already the
    direction the shared classifier expects, so this just delegates. Returns
    None for any unrecognized combination.
    """
    return classify_metric_type(result.metering_point_type, product_code)


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

    # Single metric name for all per-meter (E66) energy. The two orthogonal
    # dimensions are exposed as labels instead of baked into the metric name:
    #   direction = consumption | production
    #   segment   = cel | grid | total   (total = cel + grid)
    METRIC_NAME = 'cel_energy_kwh'

    data_points = []
    meter_id = parsed_data.meter_id
    metric_type = parsed_data.metric_type
    product_code = parsed_data.product_code or 'unknown'
    code_type = parsed_data.code_type or 'unknown'
    community_id = parsed_data.community_id or 'unknown'
    is_production_breakdown = parsed_data.is_production_breakdown

    if not metric_type:
        logger.warning("No metric type found, skipping")
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
            '__name__': METRIC_NAME,
            'project': 'cel',
            'community_id': community_id,
            'product_code': product_code,
            'code_type': code_type,
            'direction': metric_type.direction,   # consumption | production
            'segment': metric_type.segment,       # cel | grid | total
        }

        if meter_id:
            labels['meter_id'] = meter_id

        # Estimated vs measured readings split into separate series (e.g. "21").
        if obs.condition:
            labels['condition'] = obs.condition

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


