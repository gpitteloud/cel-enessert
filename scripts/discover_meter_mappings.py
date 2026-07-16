#!/usr/bin/env python3
"""
Auto-discover physical-to-virtual meter mappings

Analyzes XML files to find matching production totals between:
- Physical meters (with production total ebIX 8716867000030)
- Virtual meters (with production breakdown VSE codes)

Mappings are discovered by matching production total values.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict
from typing import Dict, Tuple
import logging
import zipfile
import yaml

logger = logging.getLogger(__name__)


def extract_production_total(xml_file: Path) -> Tuple[str, float, str]:
    """
    Extract production total from XML file

    Returns: (meter_suffix, total_kwh, metering_type) or None
    """
    try:
        tree = ET.parse(xml_file)
        root = tree.getroot()

        # Get meter ID
        meter_elem = root.find('.//{http://www.strom.ch}VSENationalID')
        if meter_elem is None:
            return None

        meter_id = meter_elem.text
        meter_suffix = meter_id[-8:] if len(meter_id) >= 8 else meter_id

        # Check if this is production
        is_production = root.find('.//{http://www.strom.ch}ProductionMeteringPoint') is not None
        if not is_production:
            return None

        # Get product code - must be ebIX Total (8716867000030)
        product = root.find('.//{http://www.strom.ch}Product')
        if product is None:
            return None

        ebix_elem = product.find('.//{http://www.strom.ch}ID/{http://www.strom.ch}ebIXCode')
        if ebix_elem is None or ebix_elem.text != '8716867000030':
            return None

        # Sum all observations to get total
        total = 0.0
        observations = root.findall('.//{http://www.strom.ch}Observation')
        for obs in observations:
            vol_elem = obs.find('.//{http://www.strom.ch}Volume')
            if vol_elem is not None:
                total += float(vol_elem.text)

        # Determine if physical or virtual based on meter ID pattern
        # Physical meters typically have different suffix patterns than virtual
        # Virtual meters for this community start with "085" in the suffix
        is_virtual = meter_suffix.startswith('085')
        metering_type = 'virtual' if is_virtual else 'physical'

        return (meter_suffix, round(total, 3), metering_type)

    except Exception as e:
        logger.debug(f"Could not extract from {xml_file.name}: {e}")
        return None


def discover_mappings(data_dir: Path, archive_dir: Path = None) -> Dict[str, str]:
    """
    Discover physical-to-virtual meter mappings by analyzing files

    Args:
        data_dir: Primary directory to scan (incoming)
        archive_dir: Optional archive directory to scan if incoming is empty

    Returns: dict mapping physical_meter_suffix -> virtual_meter_suffix
    """
    logger.info(f"Discovering meter mappings from {data_dir}")

    # Collect production totals
    physical_meters = {}  # meter_suffix -> total_kwh
    virtual_meters = {}   # meter_suffix -> total_kwh

    # Scan files (limit to recent files for efficiency)
    xml_files = sorted(data_dir.glob('*.xml'))

    # If incoming is empty or too few files, check archive
    if len(xml_files) < 100 and archive_dir and archive_dir.exists():
        logger.info(f"Not enough files in {data_dir}, checking archive: {archive_dir}")
        archive_files = sorted(archive_dir.glob('*.xml'))
        xml_files.extend(archive_files)
        logger.info(f"Found {len(archive_files)} additional files in archive")

    # If too many files, sample from recent deliveries
    if len(xml_files) > 500:
        # Get unique delivery dates
        delivery_dates = sorted(set(f.name[:8] for f in xml_files))
        # Use most recent 3 delivery dates
        recent_dates = delivery_dates[-3:]
        xml_files = [f for f in xml_files if f.name[:8] in recent_dates]
        logger.info(f"Sampling {len(xml_files)} files from {len(recent_dates)} recent deliveries")

    for xml_file in xml_files:
        result = extract_production_total(xml_file)
        if result:
            meter_suffix, total, metering_type = result
            if metering_type == 'physical':
                physical_meters[meter_suffix] = total
            elif metering_type == 'virtual':
                virtual_meters[meter_suffix] = total

    logger.info(f"Found {len(physical_meters)} physical meters with production")
    logger.info(f"Found {len(virtual_meters)} virtual meters with production")

    # Match by production totals
    mappings = {}
    tolerance = 0.1  # Allow small differences due to rounding

    for phys_meter, phys_total in physical_meters.items():
        for virt_meter, virt_total in virtual_meters.items():
            if abs(phys_total - virt_total) <= tolerance:
                mappings[phys_meter] = virt_meter
                logger.info(f"Matched: {phys_meter} (physical) <-> {virt_meter} (virtual) | total={phys_total:.3f} kWh")
                break

    logger.info(f"Discovered {len(mappings)} meter mappings")
    return mappings


def save_mappings(mappings: Dict[str, str], output_file: Path):
    """Save discovered mappings to YAML file"""
    data = {
        'meter_mappings': {
            phys: {'virtual_meter': virt}
            for phys, virt in mappings.items()
        }
    }

    with open(output_file, 'w') as f:
        f.write("# Physical to Virtual Meter Mappings\n")
        f.write("# Auto-discovered by analyzing production totals\n")
        f.write("# DO NOT EDIT - This file is automatically generated\n\n")
        yaml.dump(data, f, default_flow_style=False)

    logger.info(f"Saved {len(mappings)} mappings to {output_file}")


def get_virtual_meters_from_files(data_dir: Path, sample_size: int = 50) -> set:
    """
    Quick scan to find all virtual meter IDs in recent files

    Returns: set of virtual meter suffixes
    """
    virtual_meters = set()
    xml_files = sorted(data_dir.glob('*.xml'), reverse=True)[:sample_size]

    for xml_file in xml_files:
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()

            meter_elem = root.find('.//{http://www.strom.ch}VSENationalID')
            if meter_elem is None:
                continue

            meter_id = meter_elem.text
            meter_suffix = meter_id[-8:] if len(meter_id) >= 8 else meter_id

            # Check if virtual meter (starts with "085")
            if meter_suffix.startswith('085'):
                # Verify it has VSE production codes
                is_production = root.find('.//{http://www.strom.ch}ProductionMeteringPoint') is not None
                product = root.find('.//{http://www.strom.ch}Product')
                if is_production and product is not None:
                    vse_elem = product.find('.//{http://www.strom.ch}ID/{http://www.strom.ch}VSENationalCode')
                    if vse_elem is not None:
                        virtual_meters.add(meter_suffix)
        except:
            continue

    return virtual_meters


def _physical_meter_suffix_from_root(root) -> str:
    """Return the meter suffix if this XML is a production file reporting an
    ebIX production total (8716867000030), else None."""
    meter_elem = root.find('.//{http://www.strom.ch}VSENationalID')
    if meter_elem is None or meter_elem.text is None:
        return None

    # Must be a production metering point
    if root.find('.//{http://www.strom.ch}ProductionMeteringPoint') is None:
        return None

    # Must have ebIX production total code
    product = root.find('.//{http://www.strom.ch}Product')
    if product is None:
        return None
    ebix_elem = product.find('.//{http://www.strom.ch}ID/{http://www.strom.ch}ebIXCode')
    if ebix_elem is None or ebix_elem.text != '8716867000030':
        return None

    return meter_elem.text[-8:] if len(meter_elem.text) >= 8 else meter_elem.text


def get_physical_production_meters(data_dir: Path, archive_dir: Path = None) -> set:
    """
    Find all meter suffixes that report an ebIX production total (8716867000030).

    These are "physical" production meters. A meter that also carries VSE
    breakdown codes on the SAME suffix is self-contained (its breakdown is
    attributed to itself, not to a separate virtual meter).

    Scans loose XML files in data_dir/archive_dir AND inside archive zip files
    (once files are zipped, the ebIX total lives only inside the zip - this is
    essential when replaying extracted breakdown files whose totals are zipped).

    Returns: set of meter suffixes
    """
    physical_meters = set()

    # 1. Loose XML files in incoming
    xml_files = sorted(data_dir.glob('*.xml'))

    # If incoming has few files, also check loose XMLs in archive
    if len(xml_files) < 100 and archive_dir and archive_dir.exists():
        xml_files.extend(sorted(archive_dir.glob('*.xml')))

    for xml_file in xml_files:
        try:
            root = ET.parse(xml_file).getroot()
            suffix = _physical_meter_suffix_from_root(root)
            if suffix:
                physical_meters.add(suffix)
        except Exception:
            continue

    # 2. XML files inside archive zips
    if archive_dir and archive_dir.exists():
        for zip_path in sorted(archive_dir.glob('*.zip')):
            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    for name in zf.namelist():
                        if not name.endswith('.xml') or '_E66_' not in name:
                            continue
                        try:
                            root = ET.fromstring(zf.read(name))
                            suffix = _physical_meter_suffix_from_root(root)
                            if suffix:
                                physical_meters.add(suffix)
                        except Exception:
                            continue
            except Exception as e:
                logger.warning(f"Could not read zip {zip_path.name}: {e}")

    logger.info(f"Found {len(physical_meters)} physical production meters (ebIX total)")
    return physical_meters


def load_or_discover_mappings(data_dir: Path, cache_file: Path) -> Dict[str, str]:
    """
    Load mappings from cache, or discover if cache doesn't exist or new meters detected

    Returns: dict mapping virtual_meter_suffix -> physical_meter_suffix (reversed)
    """
    cached_mappings = None
    needs_rediscovery = False

    # Try to load cache
    if cache_file.exists():
        logger.info(f"Loading mappings from cache: {cache_file}")
        try:
            with open(cache_file, 'r') as f:
                data = yaml.safe_load(f)

            if data and 'meter_mappings' in data:
                # Store for comparison
                cached_mappings = data['meter_mappings']
                logger.info(f"Loaded {len(cached_mappings)} mappings from cache")

                # Quick check: are there new virtual meters not in cache?
                current_virtual_meters = get_virtual_meters_from_files(data_dir, sample_size=100)
                cached_virtual_meters = set(info['virtual_meter'] for info in cached_mappings.values())

                new_meters = current_virtual_meters - cached_virtual_meters
                if new_meters:
                    logger.info(f"Detected {len(new_meters)} new virtual meters: {new_meters}")
                    logger.info("Re-discovering mappings to include new members")
                    needs_rediscovery = True
                else:
                    logger.info("No new meters detected - using cached mappings")
        except Exception as e:
            logger.warning(f"Failed to load cache: {e}")
            needs_rediscovery = True
    else:
        logger.info("No cache found - discovering mappings")
        needs_rediscovery = True

    # Re-discover if needed
    if needs_rediscovery:
        # Try to discover from data_dir, also check archive if needed
        archive_dir = data_dir.parent / "archive" if data_dir.parent else None
        mappings = discover_mappings(data_dir, archive_dir)
        if mappings:
            save_mappings(mappings, cache_file)
        else:
            logger.warning("Discovery found no mappings")
            # Fall back to cache if available
            if cached_mappings:
                logger.info("Falling back to cached mappings")
                mappings = {phys: info['virtual_meter'] for phys, info in cached_mappings.items()}
            else:
                mappings = {}
    else:
        # Use cache
        mappings = {phys: info['virtual_meter'] for phys, info in cached_mappings.items()}

    # Return reversed mapping (virtual -> physical)
    reverse_map = {virt: phys for phys, virt in mappings.items()}
    logger.info(f"Using {len(reverse_map)} meter mappings")
    return reverse_map
