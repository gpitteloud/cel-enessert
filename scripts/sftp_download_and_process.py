import logging
import os
import time
import zipfile
from datetime import date, datetime, timedelta
from ftplib import FTP_TLS, error_perm
from pathlib import Path

from scripts.logger_config import configure_logging
from scripts.sdat_processor import load_config, SDATProcessor

logger = logging.getLogger(__name__)

# How far back the download looks. A wave that arrives hours -- or a day or two
# -- after its delivery is recovered; anything older is assumed already handled.
DOWNLOAD_WINDOW_DAYS = 7

# A network fault costs a whole file, and the run only comes round once a day,
# so a transfer is attempted more than once before it is given up on.
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5


def require_env(var: str):
    value = os.environ.get(var)
    if value is None:
        raise ValueError(f"Environment {var} is required")
    return value


def read_secret(name: str) -> str:
    """Read a docker secret from /run/secrets. Isolated so tests can replace it."""
    return Path(f"/run/secrets/{name}").read_text().strip()


def with_retries(what: str, action, before_retry=None):
    """Run `action`, retrying a transient failure with a growing delay.

    `error_perm` (a 5xx reply) is never retried: the server is refusing, not
    faltering, so repeating the request would only burn the delivery window --
    bad credentials and a missing file both land here.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return action()
        except error_perm:
            raise
        except Exception as e:
            if attempt == RETRY_ATTEMPTS:
                logger.error(f"{what} failed after {attempt} attempt(s): {e}")
                raise
            delay = RETRY_DELAY_SECONDS * attempt
            logger.warning(f"{what} failed ({e}); retrying in {delay}s "
                           f"(attempt {attempt + 1}/{RETRY_ATTEMPTS})")
            time.sleep(delay)
            if before_retry is not None:
                before_retry()


class FTPSession:
    """An FTP connection that can be reopened between transfers.

    Reopening is the point. A transfer interrupted by a network fault usually
    takes the control connection down with it, so repeating the RETR on the same
    connection fails instantly and a retry that does not reconnect buys nothing.
    """

    def __init__(self, host, port, user, password):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.ftps = None

    def open(self):
        with_retries(f"Connecting to {self.host}:{self.port}", self._open)

    def _open(self):
        ftps = FTP_TLS()
        ftps.connect(self.host, self.port, timeout=30)
        ftps.login(user=self.user, passwd=self.password)
        ftps.prot_p()
        self.ftps = ftps

    def reopen(self):
        """Drop the current connection and open a fresh one."""
        self.close()
        self._open()

    def close(self):
        if self.ftps is None:
            return
        try:
            self.ftps.quit()
        except Exception:
            # A connection that already died cannot be closed politely.
            pass
        self.ftps = None


def last_archived_date(archive_dir: Path) -> date:
    all_archives = sorted(archive_dir.glob('*.zip'), reverse=True)
    if all_archives:
        return datetime.strptime(all_archives[0].name[:8], "%Y%m%d").date()
    else:
        return date.fromisoformat('2026-01-01')


def archived_names(archive_dir: Path, floor: date) -> set:
    """Names already inside a daily archive, looking no further back than `floor`.
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


def download_one(session: FTPSession, filename: str, target_dir: Path) -> bool:
    """Fetch one file, atomically and with retries. True if it landed.

    Written to <name>.part and renamed, so an interrupted transfer cannot leave a
    truncated XML under its final name.
    """
    partial = target_dir / (filename + '.part')

    def fetch():
        with open(partial, "wb") as f:
            session.ftps.retrbinary("RETR " + filename, f.write)

    try:
        with_retries(f"Download of {filename}", fetch, before_retry=session.reopen)
        partial.replace(target_dir / filename)
        return True
    except error_perm as e:
        logger.error(f"Cannot download {filename}, server refused it ({e})")
        partial.unlink(missing_ok=True)
        return False
    except Exception as e:
        logger.error(f"Cannot download {filename} ({e})")
        partial.unlink(missing_ok=True)
        if session.ftps is None:
            # Reconnecting failed, so every remaining file would fail the same
            # way. End the run instead: what did not arrive is still missing and
            # still wanted, and selecting by name means the next run fetches it.
            raise
        return False


def download_sdat_files(target_dir: Path, archive_dir: Path,
                        window_days: int = DOWNLOAD_WINDOW_DAYS) -> int:
    """Download every SDAT file of the last `window_days` we do not already have.

    A file counts as already-had only once it is inside an archive zip, because
    archiving is what proves it was stored. A copy sitting in incoming or under
    failed/ proves the opposite -- it was never processed, or processing it
    failed -- so it is fetched again. That is what self-heals a truncated
    download.
    """
    host = require_env("SFTP_HOST")
    port = int(require_env("SFTP_PORT"))
    sftp_user = read_secret("sftp_user")
    password = read_secret("sftp_password")

    floor = last_archived_date(archive_dir) - timedelta(days=window_days)
    archived = archived_names(archive_dir, floor)

    logger.info(f"Download SDAT files from {floor} onward from FTP server {host} "
                f"({len(archived)} file(s) already archived)")
    session = FTPSession(host, port, sftp_user, password)
    count = 0
    try:
        logger.info(f"Connecting to {host}:{port}...")
        session.open()
        logger.info("Connected.")

        entries = with_retries("Listing the server",
                 # Materialised, so that reconnecting inside the loop below cannot leave a half-consumed listing behind.
                               lambda: list(session.ftps.mlsd()),
                               before_retry=session.reopen)
        logger.info(f"{len(entries)} entries on the server")

        for filename, facts in entries:
            # skip folders
            if facts.get("type") != "file":
                continue
            try:
                file_date = datetime.strptime(filename[:8], "%Y%m%d").date()
            except ValueError:
                logger.debug(f"Skipping {filename}, not a SDAT file with valid filename")
                continue
            if file_date < floor or filename in archived:
                continue

            logger.debug(f"Downloading {filename}")
            if download_one(session, filename, target_dir):
                count += 1

        logger.info(f"Downloaded {count} new file(s)")

    except Exception as e:
        logger.error(f"Download run stopped after {count} file(s): {e}")
    finally:
        session.close()

    return count


def quarantine_leftovers(target_dir: Path) -> int:
    """Move XML left over from a previous run into failed/, and report it.
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
