import logging
import os
import sys
from datetime import date, datetime
from ftplib import FTP_TLS
from pathlib import Path

from scripts.logger_config import configure_logging
from scripts.sdat_processor import load_config, SDATProcessor

logger = logging.getLogger(__name__)


def require_env(var: str):
    value = os.environ.get(var)
    if value is None:
        raise ValueError(f"Environment {var} is required")
    return value


def read_secret(name: str) -> str:
    """Read a docker secret from /run/secrets. Isolated so tests can replace it."""
    return Path(f"/run/secrets/{name}").read_text().strip()


def download_sdat_files(target_dir: Path, after_date: date) -> int:

    host = require_env("SFTP_HOST")
    port = int(require_env("SFTP_PORT"))
    sftp_user = read_secret("sftp_user")
    password = read_secret("sftp_password")

    logger.info(f"Download all SDAT files after {after_date} from FTP server {host}")
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
            if facts.get("type") == "file":
                try:
                    file_date = datetime.strptime(filename[:8], "%Y%m%d").date()
                except ValueError:
                    logger.info(f"Skipping {filename}, not a SDAT file with valid filename")
                    continue
                try:
                    if file_date > after_date:
                        logger.debug(f"Downloading {filename}")
                        local_file = target_dir.joinpath(filename)
                        with open(local_file, "wb") as f:
                            ftps.retrbinary("RETR " + filename, f.write)
                        count += 1
                except Exception as e:
                    logger.error(f"Cannot download {filename} ({e})")

        logger.info(f"Downloaded {count} file(s) later than '{after_date}'")
        ftps.quit()

    except Exception as e:
        logger.error(f"Error: {e}")

    return count


def last_archived_date(archive_dir: Path) -> date:
    all_archives = sorted(archive_dir.glob('*.zip'), reverse=True)
    if all_archives:
        return datetime.strptime(all_archives[0].name[:8], "%Y%m%d").date()
    else:
        return date.fromisoformat('2026-01-01')


def main():
    configure_logging()
    logger.info("CEL SDAT File downloader starting...")

    api_config = load_config("/app/config")
    job_config = api_config.get('processing', {})

    target_dir = Path(job_config.get('incoming_path'))

    # check target dir is empty
    if any(target_dir.glob("*.xml")):
        logger.error(f'Target dir {target_dir} is not empty, job aborted')
        sys.exit(3)
    else:
        last_archived = last_archived_date(Path(job_config.get('archive_path')))
        nb_files = download_sdat_files(target_dir, last_archived)
        if nb_files:
            SDATProcessor.from_config(api_config).process_sdat_files()
        else:
            logger.info(f"No new SDAT files found after {last_archived}")


if __name__ == '__main__':
    main()
