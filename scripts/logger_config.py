import logging
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

JOB_LOG = '/app/logs/job.log'


def configure_logging(log_file: str = JOB_LOG, level: int = logging.INFO) -> None:
    """Configure root logging for a job entry point. Call from main(), not on import.

    A module that opens a FileHandler at import time cannot be imported at all
    outside the container, where /app/logs does not exist -- so a missing log
    directory degrades to console only instead of raising.
    """
    handlers = [logging.StreamHandler()]
    file_error = None
    try:
        handlers.append(logging.FileHandler(log_file))
    except OSError as e:
        file_error = e

    for handler in handlers:
        handler.setFormatter(cet_formatter)
    logging.basicConfig(level=level, handlers=handlers)

    if file_error is not None:
        logging.getLogger(__name__).warning(
            f"Logging to console only, cannot open {log_file}: {file_error}")

