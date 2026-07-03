#!/usr/bin/env python3
"""
File watcher for Synology FTP directory

Monitors /volume1/ftproot for new SDAT XML files and processes them automatically.
"""

import sys
import os
import time
import logging
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import shutil
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

# Custom formatter for CET/CEST timezone (Europe/Zurich)
class CETFormatter(logging.Formatter):
    """Format log timestamps in CET/CEST timezone with automatic daylight saving"""
    def formatTime(self, record, datefmt=None):
        # Use Europe/Zurich timezone (same as CET/CEST with proper DST handling)
        dt = datetime.fromtimestamp(record.created, tz=ZoneInfo('Europe/Zurich'))
        if datefmt:
            return dt.strftime(datefmt)
        # Include timezone name (CET or CEST depending on date)
        return dt.strftime('%Y-%m-%d %H:%M:%S %Z')

# Setup logging with CET timezone
cet_formatter = CETFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

file_handler = logging.FileHandler('/app/logs/watcher.log')
file_handler.setFormatter(cet_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(cet_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)

# Import parsers
sys.path.insert(0, '/app/scripts')
from parse_sdat_e66_individual import parse_sdat_xml, transform_to_datapoints
from parse_sdat_e31_aggregated import parse_e31_xml, transform_e31_to_datapoints
from send_to_victoriametrics import send_batch
from discover_meter_mappings import load_or_discover_mappings
import yaml


def load_config():
    """Load configuration files"""
    config_dir = Path('/app/config')
    with open(config_dir / "api_config.yaml", 'r', encoding='utf-8') as f:
        api_config = yaml.safe_load(f)
    return api_config


class SDATFileHandler(FileSystemEventHandler):
    """Handle new SDAT XML files with batch processing

    Strategy: Wait for complete daily delivery (~109 files in 5 minutes)
    before processing. This avoids race conditions and allows better
    error handling for missing files.
    """

    def __init__(self, config_dir: Path, archive_dir: Path):
        self.config_dir = config_dir
        self.archive_dir = archive_dir
        self.processing = set()  # Track files currently being processed
        self.processed_files = set()  # Track files already processed (by name)

        # Batch processing state
        self.current_delivery_date = None  # Current delivery being received (YYYYMMDD)
        self.pending_files = []  # Files waiting to be processed
        self.delivery_timer = None  # Timer for batch processing delay
        self.batch_wait_seconds = 600  # Wait 10 minutes after last file before processing
        self.last_file_time = None  # Timestamp of last file received

        # Load configuration
        self.api_config = load_config()
        logger.info("Configuration loaded")

        # Auto-discover meter mappings (virtual -> physical)
        # Tries to load from cache first, discovers if cache missing/stale
        self.incoming_dir = Path("/data/incoming")
        self.cache_file = self.config_dir / "meter_mappings.yaml"

        self.meter_mappings = load_or_discover_mappings(self.incoming_dir, self.cache_file)
        if self.meter_mappings:
            logger.info(f"Loaded {len(self.meter_mappings)} meter mappings for community members")
        else:
            logger.warning("No meter mappings found - will use backward compatibility mode")

        # Load previously processed files from archive
        self._load_processed_files()

    def _load_processed_files(self):
        """Load list of already-processed files from archive directory"""
        try:
            if self.archive_dir.exists():
                # Get base filenames (without timestamp suffixes)
                for archive_file in self.archive_dir.glob("*.xml"):
                    # Remove timestamp suffix if present: filename_20260606_211530.xml -> filename.xml
                    name = archive_file.name
                    if '_' in name:
                        # Check if last part before .xml is a timestamp (YYYYMMDD_HHMMSS)
                        parts = name.rsplit('_', 2)
                        if len(parts) >= 3 and parts[-1].endswith('.xml'):
                            # Remove .xml suffix from last part
                            timestamp_part = parts[-1][:-4]
                            if parts[-2].isdigit() and len(parts[-2]) == 8 and timestamp_part.isdigit() and len(timestamp_part) == 6:
                                # This is a timestamped duplicate, get original name
                                name = '_'.join(parts[:-2]) + '.xml'

                    self.processed_files.add(name)

                logger.info(f"Loaded {len(self.processed_files)} previously processed files from archive")
        except Exception as e:
            logger.warning(f"Could not load processed files list: {e}")

    def on_created(self, event):
        """Called when a file is created - adds to batch queue"""
        if event.is_directory:
            return

        file_path = Path(event.src_path)

        # Only process XML files
        if file_path.suffix.lower() not in ['.xml']:
            return

        # Skip if already processed (deduplication check)
        if file_path.name in self.processed_files:
            logger.info(f"Skipping already-processed file: {file_path.name}")
            return

        # Extract delivery date from filename (YYYYMMDD prefix)
        delivery_date = file_path.name[:8] if len(file_path.name) >= 8 else None

        if not delivery_date or not delivery_date.isdigit():
            logger.warning(f"Cannot determine delivery date from filename: {file_path.name}")
            # Process immediately for non-standard files
            self.processing.add(str(file_path))
            self.process_file(file_path)
            return

        # Check if this is a new delivery batch
        if self.current_delivery_date != delivery_date:
            # New delivery detected
            if self.current_delivery_date and self.pending_files:
                # Process previous batch first
                logger.info(f"New delivery {delivery_date} detected while {self.current_delivery_date} pending")
                logger.info(f"Processing previous batch of {len(self.pending_files)} files immediately")
                self._process_batch()

            logger.info(f"New delivery batch started: {delivery_date}")
            self.current_delivery_date = delivery_date
            self.pending_files = []

        # Add file to pending batch
        self.pending_files.append(file_path)
        self.last_file_time = time.time()
        logger.info(f"Added to batch: {file_path.name} (batch size: {len(self.pending_files)})")

        # Cancel existing timer if any
        if self.delivery_timer:
            self.delivery_timer.cancel()

        # Start new timer - will process batch if no new files for 10 minutes
        self.delivery_timer = threading.Timer(self.batch_wait_seconds, self._process_batch)
        self.delivery_timer.daemon = True
        self.delivery_timer.start()

    def _process_batch(self):
        """Process all pending files in batch"""
        if not self.pending_files:
            logger.info("No pending files to process")
            return

        batch_size = len(self.pending_files)
        logger.info(f"=" * 80)
        logger.info(f"Processing batch: {self.current_delivery_date}")
        logger.info(f"Files in batch: {batch_size}")
        logger.info(f"=" * 80)

        # Trigger discovery before processing to ensure mappings are up-to-date
        logger.info("Refreshing meter mappings before batch processing...")
        new_mappings = load_or_discover_mappings(self.incoming_dir, self.cache_file)
        if new_mappings:
            self.meter_mappings = new_mappings
            logger.info(f"Using {len(self.meter_mappings)} meter mappings for this batch")

        # Process all files in batch
        success_count = 0
        error_count = 0

        for file_path in self.pending_files:
            # Check if file still exists (might have been manually deleted)
            if not file_path.exists():
                logger.warning(f"File no longer exists: {file_path.name}")
                error_count += 1
                continue

            # Ensure file is complete (wait if still being written)
            # This handles the case where batch timer expires while a file is still uploading
            max_wait = 5
            for i in range(max_wait):
                try:
                    last_size = file_path.stat().st_size
                    time.sleep(1)
                    if file_path.stat().st_size == last_size:
                        break  # File size stable, upload complete
                except:
                    break  # File issues, will error during processing

            # Acquire processing lock
            self.processing.add(str(file_path))

            try:
                self.process_file(file_path)
                success_count += 1
            except Exception as e:
                logger.error(f"Error processing {file_path.name}: {e}", exc_info=True)
                error_count += 1
                # Release lock on error
                self.processing.discard(str(file_path))

        # Clear batch
        self.pending_files = []
        self.current_delivery_date = None

        logger.info(f"=" * 80)
        logger.info(f"Batch processing complete")
        logger.info(f"Success: {success_count}, Errors: {error_count}")
        logger.info(f"=" * 80)

    def on_modified(self, event):
        """Called when a file is modified (some FTP uploads trigger this)"""
        # Ignore - batch processing handles everything via on_created
        # This prevents duplicate processing when FTP triggers both created and modified events
        pass

    def process_file(self, file_path: Path):
        """Process a single XML file (E66 or E31)
        Note: Lock should already be acquired by the caller (event handler)
        """
        try:
            logger.info(f"Processing {file_path.name}")

            # Detect file type by checking for E31 or E66 in filename
            is_e31 = '_E31_' in file_path.name
            is_e66 = '_E66_' in file_path.name

            if is_e31:
                # Process E31 (AggregatedMeteredData) file
                parsed_data = parse_e31_xml(file_path)

                if not parsed_data or not parsed_data.get('observations'):
                    logger.warning(f"No data found in E31 file {file_path.name}")
                    return

                # Transform to data points
                data_points = transform_e31_to_datapoints(parsed_data)

            elif is_e66:
                # Process E66 (ValidatedMeteredData) file
                # Parse XML with meter mappings
                parsed_data = parse_sdat_xml(
                    file_path,
                    meter_mappings=self.meter_mappings
                )

                if not parsed_data or not parsed_data.get('observations'):
                    logger.warning(f"No data found in E66 file {file_path.name}")
                    return

                # Get full meter ID for virtual meter breakdown attribution
                user_full_meter_id = None
                if parsed_data.get('is_community_production_breakdown'):
                    # Use attributed physical meter from auto-discovery
                    attributed_meter = parsed_data.get('attributed_physical_meter')

                    if attributed_meter is None:
                        meter_id = parsed_data.get('meter_id', '')
                        virtual_meter_suffix = meter_id[-8:] if len(meter_id) >= 8 else None
                        logger.error(f"Unknown virtual meter {virtual_meter_suffix} - no mapping found in auto-discovery. Skipping file.")
                        logger.error(f"This indicates a new member was added. Run discovery manually or wait for next batch.")
                        return

                    user_full_meter_id = f"CH101110123450000000000000{attributed_meter}"
                    logger.info(f"Attributing virtual meter data to physical meter: {attributed_meter}")

                # Transform to data points
                data_points = transform_to_datapoints(parsed_data, user_meter_id=user_full_meter_id)

            else:
                logger.warning(f"Unknown file type (neither E31 nor E66): {file_path.name}")
                return

            if not data_points:
                logger.warning(f"No data points generated from {file_path.name}")
                return

            # Send to VictoriaMetrics
            vm_url = self.api_config['victoriametrics']['url']
            batch_size = self.api_config['processing'].get('batch_size', 1000)

            success_count, error_count = send_batch(data_points, vm_url, batch_size)

            if error_count == 0:
                logger.info(f"Successfully processed {file_path.name} ({success_count} data points)")

                # Archive the file
                self.archive_file(file_path)
            else:
                logger.error(f"Failed to process {file_path.name} ({error_count} errors)")

        except Exception as e:
            logger.error(f"Error processing {file_path.name}: {e}", exc_info=True)

        finally:
            # Always release lock, even if we returned early
            self.processing.discard(str(file_path))

    def archive_file(self, file_path: Path):
        """Move processed file to archive"""
        try:
            archive_path = self.archive_dir / file_path.name

            # If file already exists in archive, add timestamp
            if archive_path.exists():
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                archive_path = self.archive_dir / f"{file_path.stem}_{timestamp}{file_path.suffix}"

            # Use copy + remove for cross-filesystem compatibility
            shutil.copy2(str(file_path), str(archive_path))
            logger.info(f"Copied {file_path.name} to {archive_path}")

            # Remove original file
            file_path.unlink()
            logger.info(f"Removed original file: {file_path.name}")

            # Mark file as processed to prevent future duplicate processing
            self.processed_files.add(file_path.name)

        except Exception as e:
            logger.error(f"Failed to archive {file_path.name}: {e}", exc_info=True)


def cleanup_duplicates(watch_dir: Path, processed_files: set):
    """
    Check for pre-existing files in watch directory that are already archived.
    Removes duplicates to keep ftproot clean.
    """
    duplicates_found = 0
    duplicates_removed = 0

    try:
        for file_path in watch_dir.glob("*.xml"):
            if file_path.name in processed_files:
                duplicates_found += 1
                try:
                    logger.info(f"Removing duplicate file: {file_path.name} (already in archive)")
                    file_path.unlink()
                    duplicates_removed += 1
                except Exception as e:
                    logger.error(f"Failed to remove duplicate {file_path.name}: {e}")

        if duplicates_found > 0:
            logger.info(f"Startup cleanup: Removed {duplicates_removed}/{duplicates_found} duplicate files")
        else:
            logger.info("Startup cleanup: No duplicate files found")

    except Exception as e:
        logger.error(f"Error during startup cleanup: {e}")


def main():
    """Main watcher loop"""
    logger.info("CEL SDAT File Watcher starting...")

    # Paths
    watch_dir = Path("/data/incoming")
    config_dir = Path("/app/config")
    archive_dir = Path("/data/archive")

    # Ensure directories exist
    archive_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Watching directory: {watch_dir}")
    logger.info(f"Archive directory: {archive_dir}")

    # Create event handler
    event_handler = SDATFileHandler(config_dir, archive_dir)

    # Clean up any pre-existing duplicates in ftproot
    cleanup_duplicates(watch_dir, event_handler.processed_files)

    # Process any existing files in the directory on startup
    logger.info("Scanning for existing files in watch directory...")
    existing_files = list(watch_dir.glob("*.xml"))
    if existing_files:
        logger.info(f"Found {len(existing_files)} existing XML files to process")
        for xml_file in existing_files:
            if xml_file.name not in event_handler.processed_files:
                logger.info(f"Queuing existing file: {xml_file.name}")
                # Simulate a file creation event for existing files
                class FakeEvent:
                    def __init__(self, path):
                        self.src_path = str(path)
                        self.is_directory = False
                event_handler.on_created(FakeEvent(xml_file))
            else:
                logger.info(f"Skipping already-processed file: {xml_file.name}")
    else:
        logger.info("No existing files found in watch directory")

    # Create observer
    observer = Observer()
    observer.schedule(event_handler, str(watch_dir), recursive=False)
    observer.start()

    logger.info("Watcher started. Monitoring for new SDAT files...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        observer.stop()

    observer.join()
    logger.info("Watcher stopped")


if __name__ == '__main__':
    main()
