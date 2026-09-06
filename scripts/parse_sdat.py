#!/usr/bin/env python3
"""
Single entry point for turning an SDAT file into storable rows.

Reading the XML lives in sdat_header; this owns the E66/E31 decision and hands
the FileHeader to the matching decoder. The document type comes from the file's
content (InstanceDocument/DocumentType/ebIXCode), NOT from the filename, so a
mis-named or renamed file is still routed correctly.
"""
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.models import ParseResult
from scripts.parse_sdat_e31_aggregated import parse_e31
from scripts.parse_sdat_e66_individual import parse_e66
from scripts.sdat_header import FileHeader, parse_header

logger = logging.getLogger(__name__)


def metered_data_from_header(header: FileHeader, meter_mappings: dict = None,
                             self_contained_meters: set = None) -> ParseResult:
    """Dispatch an already-read file to the E66 or E31 decoder.

    The batch path uses this directly: it has read every header once already, and
    reading a delivery twice is the cost this split exists to avoid.
    """
    if header.document_type == 'E66':
        return parse_e66(header, meter_mappings=meter_mappings,
                         self_contained_meters=self_contained_meters)
    if header.document_type == 'E31':
        return parse_e31(header)
    logger.error(f"{header.file_name}: unsupported or missing DocumentType "
                 f"(ebIXCode={header.document_type!r})")
    return None


def parse_sdat(xml_file, meter_mappings: dict = None,
               self_contained_meters: set = None) -> ParseResult:
    """Read one SDAT file and decode it, for a caller holding only a path.

    Returns:
        MeteredData (document_type 'E66' or 'E31'); a SkippedDocument when the
        file is valid but deliberately not ingested (see parse_e66); or None if
        the file cannot be read, is an unsupported document type, or cannot be
        attributed.
    """
    xml_file = Path(xml_file)
    try:
        data = xml_file.read_bytes()
    except OSError as e:
        logger.error(f"{xml_file.name}: cannot read: {e}")
        return None
    return parse_sdat_bytes(
        data, xml_file.name, meter_mappings=meter_mappings,
        self_contained_meters=self_contained_meters)


def parse_sdat_bytes(data: bytes, filename: str, meter_mappings: dict = None,
                     self_contained_meters: set = None) -> ParseResult:
    """Same as parse_sdat, but from bytes already in memory.

    For XML that is not a file on disk -- an archive zip member read with
    `ZipFile.read()`, most usefully -- so it can be parsed without extracting it
    first.
    """
    try:
        header = parse_header(ET.fromstring(data), filename)
    except ET.ParseError as e:
        logger.error(f"{filename}: XML parse error: {e}")
        return None
    except ValueError as e:
        logger.error(f"{filename}: cannot read header: {e}")
        return None
    return metered_data_from_header(
        header, meter_mappings=meter_mappings,
        self_contained_meters=self_contained_meters)
