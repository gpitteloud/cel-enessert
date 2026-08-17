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

