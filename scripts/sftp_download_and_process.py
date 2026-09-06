import logging
import os
import zipfile
from datetime import date, datetime, timedelta
from ftplib import FTP_TLS
from pathlib import Path

from scripts.logger_config import configure_logging
from scripts.sdat_processor import load_config, SDATProcessor

logger = logging.getLogger(__name__)

# How far back the download looks. A wave that arrives hours -- or a day or two
# -- after its delivery is recovered; anything older is assumed already handled.
DOWNLOAD_WINDOW_DAYS = 7


def require_env(var: str):
    value = os.environ.get(var)
    if value is None:
        raise ValueError(f"Environment {var} is required")
    return value


def read_secret(name: str) -> str:
    """Read a docker secret from /run/secrets. Isolated so tests can replace it."""
    return Path(f"/run/secrets/{name}").read_text().strip()


def last_archived_date(archive_dir: Path) -> date:
    all_archives = sorted(archive_dir.glob('*.zip'), reverse=True)
    if all_archives:
        return datetime.strptime(all_archives[0].name[:8], "%Y%m%d").date()
    else:
        return date.fromisoformat('2026-01-01')


def archived_names(archive_dir: Path, floor: date) -> set:
    """Names already inside a daily archive, looking no further back than `floor`.

    Bounded on purpose: one zip lands per day, so scanning them all would be
    hundreds of opens within a year. It is also unnecessary -- a batch is
    archived into a zip named after the file's own YYYYMMDD prefix, so a
    candidate with prefix P can only be inside P.zip, and candidates start at
    the floor. That is at most DOWNLOAD_WINDOW_DAYS + 1 opens, forever. Zip
    names sort as dates, so the rest are excluded by a string comparison
    without being opened.
    """
    floor_prefix = floor.strftime('%Y%m%d')
    names = set()
    for zip_path in sorted(archive_dir.glob('*.zip')):
        prefix = zip_path.name[:8]
        if not prefix.isdigit() or prefix < floor_prefix:
            continue
        try:
            with zipfile.ZipFile(zip_path) as zf:
                # namelist() reads the central directory only, no decompression.
                names.update(Path(n).name for n in zf.namelist())
        except (OSError, zipfile.BadZipFile) as e:
            logger.error(f"Cannot read archive {zip_path.name} ({e}); "
                         f"its files may be downloaded again")
    return names


def download_sdat_files(target_dir: Path, archive_dir: Path,
                        window_days: int = DOWNLOAD_WINDOW_DAYS) -> int:
    """Download every SDAT file of the last `window_days` we do not already have.

    Selection is by file NAME, not by date. Selecting by date lost data: the
    cutoff was the newest archive zip, i.e. a delivery date, so the moment run N
    archived 20260807.zip every file still to arrive that day failed
    `> 20260807` and was never downloaded again -- silently, permanently. Real
    deliveries arrive in up to four waves, as late as 16:24, so no schedule can
    avoid that; per-file identity can.

    Files under failed/ deliberately do NOT count as known, so a fresh copy is
    fetched on the next run. That is what self-heals a truncated download.
    """
    host = require_env("SFTP_HOST")
    port = int(require_env("SFTP_PORT"))
    sftp_user = read_secret("sftp_user")
    password = read_secret("sftp_password")

    floor = last_archived_date(archive_dir) - timedelta(days=window_days)
    known = archived_names(archive_dir, floor)
    known.update(p.name for p in target_dir.glob('*.xml'))

    logger.info(f"Download SDAT files from {floor} onward from FTP server {host} "
                f"({len(known)} file(s) already present or archived)")
    count = 0
    try:
        ftps = FTP_TLS()
        logger.info(f"Connecting to {host}:{port}...")
        ftps.connect(host, port, timeout=30)
        ftps.login(user=sftp_user, passwd=password)
        ftps.prot_p()
        logger.info("Connected.")

        for filename, facts in ftps.mlsd():
            # skip folders
            if facts.get("type") != "file":
                continue
            try:
                file_date = datetime.strptime(filename[:8], "%Y%m%d").date()
            except ValueError:
                logger.debug(f"Skipping {filename}, not a SDAT file with valid filename")
                continue
            if file_date < floor or filename in known:
                continue

            # Download to <name>.part and rename, so an interrupted transfer
            # cannot leave a truncated XML under its final name: that file would
            # parse to nothing, get archived, and then mask the good copy.
            partial = target_dir / (filename + '.part')
            try:
                logger.debug(f"Downloading {filename}")
                with open(partial, "wb") as f:
                    ftps.retrbinary("RETR " + filename, f.write)
                partial.replace(target_dir / filename)
                count += 1
            except Exception as e:
                logger.error(f"Cannot download {filename} ({e})")
                partial.unlink(missing_ok=True)

        logger.info(f"Downloaded {count} new file(s)")
        ftps.quit()

    except Exception as e:
        logger.error(f"Error: {e}")

    return count


def quarantine_leftovers(target_dir: Path) -> int:
    """Move XML left over from a previous run into failed/, and report it.

    Aborting instead -- what this replaces -- meant a single unparseable file
    stopped every later delivery: nothing was downloaded, so the "kept in the
    source folder for retry" files had no next delivery to be retried with.

    failed/ is a subdirectory of the incoming folder, not a sibling: /data
    itself is not a mount, only /data/incoming and /data/archive are, so a
    sibling would not survive the container being recreated. Every scan in the
    codebase globs non-recursively, so a subdirectory is invisible to them.
    """
    leftovers = sorted(target_dir.glob('*.xml'))
    if not leftovers:
        return 0

    failed_dir = target_dir / 'failed'
    failed_dir.mkdir(exist_ok=True)
    for f in leftovers:
        logger.warning(f"Not processed by the previous run: {f.name}")
        # replace(), so the same file failing again overwrites rather than raising
        f.replace(failed_dir / f.name)
    logger.warning(f"Moved {len(leftovers)} unprocessed file(s) to {failed_dir}")
    return len(leftovers)


def main():
    configure_logging()
    logger.info("CEL SDAT File downloader starting...")

    api_config = load_config("/app/config")
    job_config = api_config.get('processing', {})

    target_dir = Path(job_config.get('incoming_path'))
    archive_dir = Path(job_config.get('archive_path'))
    window_days = int(job_config.get('download_window_days', DOWNLOAD_WINDOW_DAYS))

    quarantine_leftovers(target_dir)
    nb_files = download_sdat_files(target_dir, archive_dir, window_days)
    if nb_files:
        SDATProcessor.from_config(api_config).process_sdat_files()
    else:
        logger.info("No new SDAT files on the server")


if __name__ == '__main__':
    main()
