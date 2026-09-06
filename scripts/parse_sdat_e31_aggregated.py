#!/usr/bin/env python3
"""
E31 (AggregatedMeteredData_1.3) -> MeteredData: community aggregates.

Everything E31 needs is already single-valued in the FileHeader, so this is a
straight projection onto the fields the aggregate table stores. There is no
meter to attribute: the aggregate belongs to the community, not to a meter.
"""

import logging
from typing import Optional

from scripts.models import MeteredData
from scripts.sdat_header import FileHeader

logger = logging.getLogger(__name__)


def parse_e31(header: FileHeader) -> Optional[MeteredData]:
    """Turn an E31 FileHeader into the community rows it should be stored as."""
    if header.metric_type is None:
        logger.warning(f"{header.file_name}: unclassified E31 series "
                       f"(flow={header.flow_characteristic!r}, "
                       f"product={header.product_code!r})")

    return MeteredData(
        document_type='E31',
        filename=header.file_name,
        observations=header.observations,
        product_code=header.product_code,
        code_type=header.code_type,
        community_id=header.community_id,
        metric_type=header.metric_type,
        community_type=header.community_type,
        grid_area=header.grid_area,
    )
