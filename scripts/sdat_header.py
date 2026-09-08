#!/usr/bin/env python3
"""File-level metadata for one SDAT document, read in a single pass.

"Header" here means everything about a file except its readings: the
HeaderInformation element plus the single-valued MeteringData fields (meter,
product, community, period). A file is exactly one series -- one MeteringData,
one Product, one metering point -- so one FileHeader describes it completely.

Every path is anchored at the document root because the header element is named
after the document type (``ValidatedMeteredData_HeaderInformation`` for E66,
``AggregatedMeteredData_HeaderInformation`` for E31); anchoring on it would need
two code paths for identical content.

The observations are carried on the same object: reading a file is the expensive
part, and the batch needs both the metadata (to classify meters and to record
provenance) and the values (to pair virtual meters and to validate the
delivery), so nothing here is ever parsed twice.
"""
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from scripts.models import (MetricType, Observation, classify_metric_type,
                            flow_to_direction, is_rcp)
from scripts.sdat_xml import NS, extract_product_code, extract_resolution_minutes, parse_observations

logger = logging.getLogger(__name__)

# The provider's clock: every delivered name starts with YYYYMMDD_HHMMSS.
FILENAME_TS_LENGTH = 15
FILENAME_TS_FORMAT = '%Y%m%d_%H%M%S'


@dataclass
class FileHeader:
    """What one SDAT file is, plus the readings it carries."""
    file_name: str
    ts: Optional[datetime] = None            # from the filename, not the content
    delivery: Optional[str] = None           # YYYYMMDD filename prefix
    document_type: Optional[str] = None      # E66 | E31
    document_id: Optional[str] = None
    creation: Optional[datetime] = None
    business_reason: Optional[str] = None    # C40 (CEL) | E88 (RCP)
    reason_code_type: Optional[str] = None   # VSENationalCode | ebIXCode
    sender_role: Optional[str] = None        # MDR | DEA
    receiver_role: Optional[str] = None      # CEM (CEL) | DEC (RCP)
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    file_meter_id: Optional[str] = None      # the file's OWN meter, virtual or not
    metering_point_type: Optional[str] = None    # consumption | production | None (E31)
    flow_characteristic: Optional[str] = None    # E17 | E18, E31 only
    community_id: Optional[str] = None       # absent on RCP files
    # E31 only, and stored on the aggregate rows rather than in the header table.
    community_type: Optional[str] = None
    grid_area: Optional[str] = None
    product_code: Optional[str] = None
    code_type: Optional[str] = None
    metric_type: Optional[MetricType] = None
    resolution_minutes: Optional[int] = None
    interval_start: Optional[str] = None     # ISO-8601, base of the observation clock
    rcp: bool = False
    observations: List[Observation] = field(default_factory=list, repr=False)
    # Which meter the rows were finally stored under: a virtual meter's breakdown
    # belongs to its physical twin, so this differs from file_meter_id there.
    attributed_meter_id: Optional[str] = None

    @property
    def report_period(self) -> Tuple[Optional[datetime], Optional[datetime]]:
        """The group a file belongs to. ReportPeriod == Interval in every file."""
        return (self.period_start, self.period_end)

    @property
    def direction(self) -> Optional[str]:
        return self.metric_type.direction if self.metric_type else None

    @property
    def segment(self) -> Optional[str]:
        return self.metric_type.segment if self.metric_type else None

    @property
    def observation_count(self) -> int:
        return len(self.observations)

    @property
    def values(self) -> Tuple[Decimal, ...]:
        """The whole reading vector, for exact comparison between two files."""
        return tuple(obs.value for obs in self.observations)


def _text(root, path: str) -> Optional[str]:
    elem = root.find(path, NS)
    return elem.text if elem is not None else None


def _timestamp(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace('Z', '+00:00'))


def ts_from_filename(file_name: str) -> Tuple[Optional[datetime], Optional[str]]:
    """(timestamp, delivery) from a YYYYMMDD_HHMMSS name, or (None, None).

    Derived without reading the file, so re-ingesting the same file writes the
    same header row -- which is what makes DEDUP UPSERT KEYS(ts, file_name)
    idempotent.
    """
    try:
        ts = datetime.strptime(file_name[:FILENAME_TS_LENGTH], FILENAME_TS_FORMAT)
    except ValueError:
        return None, None
    return ts, file_name[:8]


def _business_reason(root) -> Tuple[Optional[str], Optional[str]]:
    """(code, element name) from BusinessReasonType -- the domain discriminator.

    The child element name is what separates the domains; the codeListID
    attribute says "VSE" even on an RCP file whose child is <ebIXCode>E88</>.
    """
    for code_type in ('ebIXCode', 'VSENationalCode'):
        code = _text(root, f'.//rsm:BusinessReasonType/rsm:{code_type}')
        if code is not None:
            return code, code_type
    return None, None


def _metering_point(root) -> Tuple[Optional[str], Optional[str]]:
    """(meter id, 'consumption'|'production'), or (None, None) for an aggregate."""
    for point_type in ('consumption', 'production'):
        meter_id = _text(root,f'.//rsm:{point_type.capitalize()}MeteringPoint/rsm:VSENationalID')
        if meter_id is not None:
            return meter_id, point_type
    return None, None


def parse_header(root, file_name: str) -> FileHeader:
    """Read one document into a FileHeader, observations included.

    Raises ValueError for a document that cannot be turned into observations
    (no MeteringData, no resolution, no interval start) or that belongs to an
    unknown domain. Callers treat that as a failed file.
    """
    metering_data = root.find('.//rsm:MeteringData', NS)
    if metering_data is None:
        raise ValueError('no MeteringData element')

    resolution_minutes = extract_resolution_minutes(metering_data)
    if resolution_minutes is None:
        raise ValueError('no usable Resolution')

    interval_start = _text(metering_data, './/rsm:Interval/rsm:StartDateTime')
    if interval_start is None:
        raise ValueError('no Interval start')

    business_reason, reason_code_type = _business_reason(root)
    file_meter_id, metering_point_type = _metering_point(root)
    product_code, code_type = extract_product_code(metering_data)
    flow_characteristic = _text(
        root, './/rsm:AggregationCriteria/rsm:FlowCharacteristic')
    ts, delivery = ts_from_filename(file_name)

    # E66 states the direction as a metering point type, E31 as a flow
    # characteristic; a file carries exactly one of the two.
    direction = metering_point_type or flow_to_direction(flow_characteristic)

    return FileHeader(
        file_name=file_name,
        ts=ts,
        delivery=delivery,
        document_type=_text(root, './/rsm:InstanceDocument/rsm:DocumentType/rsm:ebIXCode'),
        document_id=_text(root, './/rsm:InstanceDocument/rsm:DocumentID'),
        creation=_timestamp(_text(root, './/rsm:InstanceDocument/rsm:Creation')),
        business_reason=business_reason,
        reason_code_type=reason_code_type,
        sender_role=_text(root, './/rsm:Sender/rsm:Role'),
        receiver_role=_text(root, './/rsm:Receiver/rsm:Role'),
        period_start=_timestamp(_text(root, './/rsm:ReportPeriod/rsm:StartDateTime')),
        period_end=_timestamp( _text(root, './/rsm:ReportPeriod/rsm:EndDateTime')),
        file_meter_id=file_meter_id,
        metering_point_type=metering_point_type,
        flow_characteristic=flow_characteristic,
        community_id=_text(metering_data, './/rsm:Community/rsm:CommunityID'),
        community_type=_text(metering_data, './/rsm:Community/rsm:CommunityType/rsm:VSENationalCode'),
        grid_area=_text(metering_data, './/rsm:MeteringGridArea/rsm:EICID'),
        product_code=product_code,
        code_type=code_type,
        metric_type=classify_metric_type(direction, product_code),
        resolution_minutes=resolution_minutes,
        interval_start=interval_start,
        rcp=is_rcp(business_reason),
        observations=parse_observations(metering_data, interval_start, resolution_minutes),
        attributed_meter_id=file_meter_id,
    )


def read_header(path: Path) -> FileHeader:
    """parse_header over a file on disk."""
    return parse_header(ET.parse(path).getroot(), Path(path).name)


def load_headers(paths: Iterable[Path]) -> Dict[str, FileHeader]:
    """Read a whole batch into {file_name: FileHeader}.

    A file that cannot be read is left out rather than aborting the batch; the
    caller sees the missing name and fails that file alone.
    """
    headers = {}
    for path in paths:
        path = Path(path)
        try:
            headers[path.name] = read_header(path)
        except (OSError, ET.ParseError, ValueError) as e:
            logger.error(f"{path.name}: cannot read header: {e}")
    return headers
