import logging
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from scripts.delivery_report import report_delivery
from scripts.discover_meter_mappings import (load_cached_mappings,
                                             log_mapping_changes,
                                             resolve_periods, save_mappings)
from scripts.logger_config import configure_logging
from scripts.models import SkippedDocument, MeteredData
from scripts.parse_sdat import metered_data_from_header
from scripts.questdb_writer import DEFAULT_DSN as QUESTDB_DEFAULT_DSN, QuestDBWriter
from scripts.sdat_header import load_headers, read_header

logger = logging.getLogger(__name__)


class FileOutcome(Enum):
    """Result of handling one file, and what the batch loop should do with it.

    SKIPPED is NOT an error: the file was understood and deliberately not
    ingested (a mapped virtual meter's duplicate production total, ~9 per
    delivery). It must still be archived, otherwise it stays in the incoming
    folder forever and gets re-examined -- and re-reported -- every delivery.
    """
    INGESTED = 'ingested'   # written to QuestDB    -> archive
    SKIPPED = 'skipped'     # intentional, expected -> archive
    FAILED = 'failed'       # real problem          -> keep for retry

    @property
    def archivable(self) -> bool:
        return self is not FileOutcome.FAILED


def group_by_date(xml_files):
    """Group files by their YYYYMMDD delivery prefix, each batch in filename order.

    The sort by filename means delivery time, so that we reproduce the order of the provider.
    """
    batches = defaultdict(list)
    for f in sorted(xml_files, key=lambda p: p.name):
        date_prefix = f.name[:8]
        batches[date_prefix].append(f)
    return batches


class SDATProcessor:
    """Ingest a folder of SDAT files, one batch per delivery date.

    The constructor only assigns: no network, no filesystem scan, no XML parse.
    Everything the run needs is either passed in or resolved by `from_config`,
    which is what production uses.
    """

    def __init__(self, incoming_dir: Path, archive_dir: Path, writer,
                 mapping_cache_file: Path = None, meter_mappings: dict = None,
                 self_contained_meters: set = None):
        self.incoming_dir = Path(incoming_dir)
        self.archive_dir = Path(archive_dir)
        self.questdb = writer
        self.mapping_cache_file = mapping_cache_file
        # None means "use what the batch discovers, falling back to the recorded
        # mappings"; an empty dict/set is an explicit choice and is left alone.
        self.meter_mappings = meter_mappings
        self.self_contained_meters = self_contained_meters

    @classmethod
    def from_config(cls, api_config):
        """Build the processor production uses: paths from config, a real writer."""
        job_config = api_config.get('processing', {})
        writer = QuestDBWriter(
            api_config.get('questdb', {}).get('dsn') or QUESTDB_DEFAULT_DSN)
        logger.info("QuestDB writer ready")
        return cls(
            incoming_dir=Path(job_config.get('incoming_path')),
            archive_dir=Path(job_config.get('archive_path')),
            writer=writer,
            mapping_cache_file=Path(job_config.get('meter_mapping_file')),
        )

    def cached_mappings(self) -> dict:
        """The recorded mappings, used only where discovery is not conclusive."""
        if self.meter_mappings is not None:
            return self.meter_mappings
        if self.mapping_cache_file:
            return load_cached_mappings(self.mapping_cache_file)
        return {}

    def resolve_meters(self, headers) -> dict:
        """{report period: PeriodResolution} for one delivery.

        Per report period, not per delivery: a delivery can carry two periods,
        and a monthly total must never be compared with a 5-day one.
        Discovery runs on the delivery itself, so a new member is picked up
        without the cache; the cache only covers a period discovery could not
        resolve.
        """
        cached = self.cached_mappings()
        resolved = resolve_periods(headers, cached)

        trusted = {}
        for resolution in resolved.values():
            if resolution.trusted:
                trusted.update(resolution.mappings)
        if trusted:
            log_mapping_changes(trusted, cached)
            if self.mapping_cache_file:
                save_mappings(trusted, self.mapping_cache_file)
        return resolved

    def meters_for(self, header, resolved: dict):
        """The mappings and self-contained meters that apply to one file.

        A period discovery never saw -- an RCP file, or a single file processed on
        its own -- falls back to the recorded mappings.
        """
        resolution = resolved.get(header.report_period)
        if resolution is None:
            return (self.cached_mappings(), self.self_contained_meters or set())
        return resolution.mappings, resolution.self_contained

    def process_sdat_files(self):
        logger.info(f"=" * 80)
        logger.info(f"Start batch processing")
        logger.info(f"Reading files from {self.incoming_dir}")
        batches = group_by_date(self.incoming_dir.glob("*.xml"))
        logger.info(f"Found {len(batches)} distinct delivery dates")
        logger.info(f"=" * 80)

        for delivery_date in sorted(batches):
            batch = batches[delivery_date]
            self.process_sdat_files_for_date(batch, delivery_date)

        logger.info(f"=" * 80)
        logger.info(f"Batch processing complete")
        logger.info(f"=" * 80)


    def process_sdat_files_for_date(self, batch: list[Path], delivery_date: Any):
        success_count = 0
        skipped_count = 0
        error_count = 0
        archivable_files = []
        failed_files = []

        logger.info(f"-" * 80)
        logger.info(f"Processing {len(batch)} files delivered on {delivery_date}")
        logger.info(f"-" * 80)

        # One read for the whole delivery: discovery, attribution and provenance
        # all work off these headers, so no file is parsed twice.
        headers = load_headers(batch)
        resolved = self.resolve_meters(headers.values())

        for file_path in batch:
            # Check if file still exists (might have been manually deleted)
            if not file_path.exists():
                logger.warning(f"File no longer exists: {file_path.name}")
                error_count += 1
                failed_files.append(file_path.name)
                continue

            try:
                header = headers.get(file_path.name)
                if header is None:
                    # load_headers already logged why it could not be read.
                    outcome = FileOutcome.FAILED
                else:
                    outcome = self.process_sdat_file(file_path, header, resolved)

                # Ingested AND intentionally-skipped files are both archived.
                if outcome.archivable:
                    archivable_files.append(file_path)
                    if outcome is FileOutcome.INGESTED:
                        success_count += 1
                    else:
                        skipped_count += 1
                else:
                    # Real failure - leave file in source folder for retry
                    logger.warning(f"File not processed, keeping in source folder: {file_path.name}")
                    error_count += 1
                    failed_files.append(file_path.name)
            except Exception as e:
                logger.error(f"Error processing {file_path.name}: {e}", exc_info=True)
                error_count += 1
                failed_files.append(file_path.name)

        # Archive processed files as a zip (only real failures stay in the source folder, so incoming ends up empty)
        if archivable_files:
            archive_batch_as_zip(archivable_files, delivery_date, self.archive_dir)

        try:
            report_delivery(delivery_date, headers.values(), resolved, failed_files)
        except Exception as e:
            # A diagnostic must never fail the ingestion it describes.
            logger.error(f"Could not report delivery {delivery_date}: {e}",
                         exc_info=True)

        logger.info(f"-" * 80)
        logger.info(f"{delivery_date} batch complete")
        logger.info(f"Ingested: {success_count}, Skipped by design: {skipped_count}, Errors: {error_count}")
        logger.info(f"-" * 80)
        return success_count


    def process_sdat_file(self, file_path: Path, header=None,
                          resolved: dict = None) -> FileOutcome:
        """Process a single XML file (E66 or E31)

        `header` and `resolved` come from the batch, which has read every header
        once already; given neither, the file is read on its own and the recorded
        mappings apply.

        Returns:
            FileOutcome.INGESTED - parsed and written to QuestDB.
            FileOutcome.SKIPPED  - valid but deliberately not ingested (expected;
                                   archived like an ingested file, not an error).
            FileOutcome.FAILED   - could not be handled; file stays in the source
                                   folder for retry and is NOT archived.
        """
        logger.info(f"Processing {file_path.name}")
        if header is None:
            try:
                header = read_header(file_path)
            except (OSError, ET.ParseError, ValueError) as e:
                logger.error(f"{file_path.name}: cannot read header: {e}")
                # No header means no header row; the attempt is still logged, so
                # "which files failed?" is answerable for these too.
                self.questdb.log_ingest(
                    delivery=file_path.name[:8], file_name=file_path.name,
                    document_type=None, rows_written=0, outcome='failed')
                return FileOutcome.FAILED

        try:
            outcome, rows_written = self._ingest(header, resolved or {})
        except Exception as e:
            logger.error(f"Error processing {file_path.name}: {e}", exc_info=True)
            outcome, rows_written = FileOutcome.FAILED, 0

        # Provenance in one place: what the file is, then what happened to it.
        self.questdb.log_file_header(header)
        self.questdb.log_ingest(
            delivery=header.delivery, file_name=header.file_name,
            document_type=header.document_type, rows_written=rows_written,
            outcome=outcome.value)
        return outcome


    def _ingest(self, header, resolved: dict):
        """Parse one file's header into rows and write them: (outcome, rows)."""
        meter_mappings, self_contained = self.meters_for(header, resolved)
        # Dispatch by document content (E66 vs E31), attribution included
        parsed_data = metered_data_from_header(
            header, meter_mappings=meter_mappings,
            self_contained_meters=self_contained)

        # total production is on both virtual and physical meters, so skip the virtual one
        if isinstance(parsed_data, SkippedDocument):
            logger.info(f"Skipped by design: {header.file_name} ({parsed_data.reason})")
            return FileOutcome.SKIPPED, 0

        if parsed_data is None:
            logger.warning(f"Could not parse (unknown type or invalid): {header.file_name}")
            return FileOutcome.FAILED, 0

        if not parsed_data.observations:
            logger.warning(f"No data found in {parsed_data.document_type} file {header.file_name}")
            return FileOutcome.FAILED, 0

        rows = self._write_questdb(parsed_data)
        if rows is None:
            logger.error(f"Failed to process {header.file_name}: QuestDB write failed")
            return FileOutcome.FAILED, 0

        logger.info(f"Successfully processed {header.file_name}")
        return FileOutcome.INGESTED, rows


    def _write_questdb(self, parsed_data: MeteredData):
        """Write one parsed document to QuestDB; rows written, or None if it failed.

        A failure is propagated rather than swallowed: QuestDB is the only store,
        so archiving a file whose write failed would lose its data silently. The
        caller marks it FAILED, which leaves it in the incoming folder for the
        next delivery to retry.
        """
        try:
            if parsed_data.document_type == 'E31':
                rows = self.questdb.write_e31(parsed_data)
            else:
                rows = self.questdb.write_e66(parsed_data)
        except Exception as e:
            logger.error(f"QuestDB write failed for {parsed_data.filename}: {e}",
                         exc_info=True)
            return None

        if not rows:
            # A parsed document with observations that yields no rows means the
            # writer rejected all of them (no metric_type, say). Fail otherwise
            # the file would be archived having written nothing.
            logger.warning(f"QuestDB: no rows generated from {parsed_data.filename}")
            return None

        logger.info(f"QuestDB: wrote {rows} rows from {parsed_data.filename}")
        return rows


def archive_batch_as_zip(file_paths: list, date_str: str, archive_dir: Path):
    """Archive a batch of files into a single zip file named by date (YYYYMMDD.zip)

    If a zip for this date already exists, new files are appended to it
    rather than creating a separate timestamped archive. Files already
    present in the zip are skipped to avoid duplicates.
    """
    try:
        zip_filename = f"{date_str}.zip"
        zip_path = archive_dir / zip_filename

        # Open in append mode if zip exists, otherwise create new
        if zip_path.exists():
            mode = 'a'
            # Get names already in the archive to skip duplicates
            with zipfile.ZipFile(zip_path, 'r') as existing:
                existing_names = set(existing.namelist())
            logger.info(f"Appending to existing archive: {zip_filename} ({len(existing_names)} files already present)")
        else:
            mode = 'w'
            existing_names = set()
            logger.info(f"Creating archive: {zip_filename} with {len(file_paths)} files")

        # Track which files actually get added (so we only delete those)
        added_files = []

        with zipfile.ZipFile(zip_path, mode, zipfile.ZIP_DEFLATED) as zipf:
            for file_path in file_paths:
                if not file_path.exists():
                    continue
                if file_path.name in existing_names:
                    logger.debug(f"Already in archive, skipping: {file_path.name}")
                    added_files.append(file_path)  # still safe to remove source
                    continue
                zipf.write(file_path, arcname=file_path.name)
                added_files.append(file_path)
                logger.debug(f"Added {file_path.name} to zip")

        logger.info(f"Archive updated: {zip_filename}")

        # Remove original files after successful zipping
        for file_path in added_files:
            if file_path.exists():
                file_path.unlink()
                logger.debug(f"Removed original file: {file_path.name}")

        logger.info(f"Archived {len(added_files)} files to {zip_filename}")

    except Exception as e:
        logger.error(f"Failed to create/update zip archive {date_str}.zip: {e}", exc_info=True)
        logger.warning("Files not deleted due to archiving error")



def load_config(config_path):
    """Load configuration file"""
    config_dir = Path(config_path)
    with open(config_dir / "api_config.yaml", 'r', encoding='utf-8') as f:
        api_config = yaml.safe_load(f)
    return api_config


def main():
    # standalone entry point: process files stored in /data/incoming
    configure_logging()
    api_config = load_config("/app/config")
    SDATProcessor.from_config(api_config).process_sdat_files()


if __name__ == '__main__':
    main()
