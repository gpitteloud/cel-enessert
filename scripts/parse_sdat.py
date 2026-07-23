#!/usr/bin/env python3
"""
Single entry point for parsing SDAT XML files.

Owns the XML parsing and the E66/E31 decision, then dispatches to the
format-specific decoder. The document type is determined from the file's
content (InstanceDocument/DocumentType/ebIXCode), NOT from the filename, so a
mis-named or renamed file is still routed correctly.
"""
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from models import MeteredData
from parse_sdat_e66_individual import parse_e66
from parse_sdat_e31_aggregated import parse_e31

logger = logging.getLogger(__name__)

# DocumentType/ebIXCode lives in the header of both formats
_DOC_TYPE_PATH = ('.//{http://www.strom.ch}DocumentType'
                  '/{http://www.strom.ch}ebIXCode')


def parse_sdat(xml_file: Path, meter_mappings: dict = None,
               physical_production_meters: set = None) -> Optional[MeteredData]:
    """
    Parse an SDAT XML file, dispatching to the E66 or E31 decoder by content.

    Args:
        xml_file: Path to the SDAT XML file
        meter_mappings: virtual->physical meter map (E66 only, optional)
        physical_production_meters: self-contained meter suffixes (E66 only, optional)

    Returns:
        MeteredData (document_type 'E66' or 'E31'), or None if the file cannot
        be parsed, has no DocumentType, or is an unsupported document type.
    """
    xml_file = Path(xml_file)
    try:
        root = ET.parse(xml_file).getroot()
    except ET.ParseError as e:
        logger.error(f"{xml_file.name}: XML parse error: {e}")
        return None

    doc_type_elem = root.find(_DOC_TYPE_PATH)
    doc_type = doc_type_elem.text if doc_type_elem is not None else None

    if doc_type == 'E66':
        return parse_e66(root, meter_mappings=meter_mappings,
                         physical_production_meters=physical_production_meters)
    elif doc_type == 'E31':
        return parse_e31(root)
    else:
        logger.error(f"{xml_file.name}: unsupported or missing DocumentType "
                     f"(ebIXCode={doc_type!r})")
        return None
