#!/usr/bin/env python3
"""Export the billing import file: each customer's energy over a period.

One line per customer, in customers.yaml order. Each of its meters, consumption
meters first, then production, becomes 6 blocks in the Article, Libellé and
Quantité columns, the blocks joined with '|':

    Article    (empty)                    | 4 item ids of the meter's role | (empty)
    Libellé    Point de mesure <meter id> | the 4 item names, from produits.csv | ' '
    Quantité   (empty)                    | cel | cel | cel | grid | (empty)

The last block is a blank line closing the meter, the last meter included.

Quantities are MWh with 3 decimals, summed from cel_energy over the meter's own
direction. The period is the N full calendar months before the run date, with
days in Europe/Zurich time: the bill follows the customer's calendar, not UTC.

The meters are the ones customers.yaml declares, not the customer_id stored on
the rows, so the file bills what the customer owns today.

Usage:
    python -m scripts.billing_export [--months N] [--from YYYY-MM-DD --to YYYY-MM-DD]
                                     [--output DIR] [--force]
"""

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
from zoneinfo import ZoneInfo

from scripts.logger_config import configure_logging
from scripts.meters import CONSUMPTION, PRODUCTION, Meters, load_meters
from scripts.questdb_writer import DEFAULT_DSN as QUESTDB_DEFAULT_DSN
from scripts.sdat_processor import load_config

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo('Europe/Zurich')
DEFAULT_MONTHS = 3
SLOT = timedelta(minutes=15)

HEADER = ('From', 'To', 'NumClientRomande', 'Article', 'Libellé', 'Quantité')
DATE_FORMAT = '%d.%m.%Y'
BLOCK_SEPARATOR = '|'
METER_LABEL = 'Point de mesure {}'
SPACER_LABEL = ' '     # blank line after each meter; an empty label would be dropped

# The 4 items billed per meter, by role. The first three carry the CEL energy,
# the last the grid energy.
ITEMS = {
    CONSUMPTION: ('3002', '1', '4001', '5001'),
    PRODUCTION: ('3001', '1', '4002', '5002'),
}
SEGMENTS = ('cel', 'cel', 'cel', 'grid')

# ts is the slot start, so a half-open window holds exactly the period's slots.
METER_SUMS = """
SELECT meter_id, direction, segment, sum(value), count()
FROM cel_energy
WHERE ts >= %s AND ts < %s
  AND segment IN ('cel', 'grid')
GROUP BY meter_id, direction, segment
"""


@dataclass(frozen=True)
class Period:
    first_day: date
    last_day: date

    @property
    def utc_bounds(self) -> Tuple[datetime, datetime]:
        """[first day 00:00, day after last 00:00), Zurich midnights in UTC."""
        start = datetime.combine(self.first_day, time(), LOCAL_TZ)
        end = datetime.combine(self.last_day + timedelta(days=1), time(), LOCAL_TZ)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    @property
    def expected_slots(self) -> int:
        """Slots in the period, which DST makes 4 more or fewer than days * 96."""
        start, end = self.utc_bounds
        return int((end - start) / SLOT)

    @property
    def file_stem(self) -> str:
        """import_factures_26T3 for a calendar quarter, else the two dates."""
        first, last = self.first_day, self.last_day
        if (first.day == 1 and first.month % 3 == 1
                and last == _month_end(first.year, first.month + 2)):
            return f"import_factures_{first:%y}T{(first.month - 1) // 3 + 1}"
        return f"import_factures_{first:%Y%m%d}_{last:%Y%m%d}"


def _month_end(year: int, month: int) -> date:
    first_of_next = date(year + month // 12, month % 12 + 1, 1)
    return first_of_next - timedelta(days=1)


def last_full_months(today: date, months: int) -> Period:
    """The `months` complete calendar months before the one `today` is in."""
    if months < 1:
        raise ValueError(f"months must be at least 1, got {months}")
    last_day = today.replace(day=1) - timedelta(days=1)
    index = last_day.year * 12 + last_day.month - 1 - (months - 1)
    return Period(date(index // 12, index % 12 + 1, 1), last_day)


def load_products(path) -> Dict[str, str]:
    """Item id -> name, from the accounting's id;name export.

    The accounting writes it in Windows-1252. Every id an ITEMS role uses must be
    present: a bill line without its name would be rejected at import anyway.
    """
    products = {}
    with open(path, encoding='cp1252', newline='') as f:
        for line_no, row in enumerate(csv.reader(f, delimiter=';'), start=1):
            if not row or not ''.join(row).strip():
                continue
            if len(row) < 2 or not row[0].strip():
                raise ValueError(f"{path}:{line_no}: expected id;name, got {row!r}")
            products[row[0].strip()] = row[1].strip()
    missing = sorted({i for ids in ITEMS.values() for i in ids} - set(products))
    if missing:
        raise ValueError(f"{path}: no name for item(s) {', '.join(missing)}")
    return products


def fetch_sums(conn, period: Period) -> Dict[Tuple[str, str, str], Tuple[Decimal, int]]:
    """{(meter, direction, segment): (kWh, slot count)} over the period."""
    start, end = period.utc_bounds
    with conn.cursor() as cur:
        cur.execute(METER_SUMS, (start, end))
        return {(meter, direction, segment): (Decimal(total or 0), int(count))
                for meter, direction, segment, total, count in cur.fetchall()}


def format_mwh(kwh: Decimal) -> str:
    """kWh as MWh, 3 decimals, trailing zeros dropped, decimal comma: 0,6."""
    mwh = (kwh / 1000).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
    text = format(mwh.normalize(), 'f')
    return '0' if text in ('0', '-0') else text.replace('.', ',')


def customer_meters(meters: Meters) -> Dict[str, List[Tuple[str, str]]]:
    """customer -> [(meter, role)], customers.yaml order, consumption first."""
    by_customer: Dict[str, List[Tuple[str, str]]] = {}
    for meter_id, (customer_id, role) in meters.owners.items():
        by_customer.setdefault(customer_id, []).append((meter_id, role))
    for owned in by_customer.values():
        owned.sort(key=lambda m: m[1] != CONSUMPTION)     # stable: keeps yaml order
    return by_customer


def build_rows(meters: Meters, products: Dict[str, str], sums, period: Period) -> List[tuple]:
    """One row per customer. Logs every meter whose data is incomplete."""
    expected = period.expected_slots
    rows = []
    for customer_id, owned in customer_meters(meters).items():
        articles, labels, quantities = [], [], []
        for meter_id, role in owned:
            energy = {}
            for segment in ('cel', 'grid'):
                kwh, count = sums.get((meter_id, role, segment), (Decimal(0), 0))
                if count < expected:
                    logger.warning(f"Customer {customer_id}, {role} meter {meter_id}: "
                                   f"{segment} has {count}/{expected} slots, billed "
                                   f"on what is stored")
                energy[segment] = format_mwh(kwh)
            items = ITEMS[role]
            articles += ['', *items, '']
            labels += [METER_LABEL.format(meter_id), *(products[i] for i in items), SPACER_LABEL]
            quantities += ['', *(energy[s] for s in SEGMENTS), '']
        rows.append((period.first_day.strftime(DATE_FORMAT),
                     period.last_day.strftime(DATE_FORMAT),
                     customer_id,
                     BLOCK_SEPARATOR.join(articles),
                     BLOCK_SEPARATOR.join(labels),
                     BLOCK_SEPARATOR.join(quantities)))
    return rows


def write_csv(path: Path, rows: Sequence[tuple]) -> None:
    """';'-separated, UTF-8 with BOM and CRLF, as the accounting import reads it.

    Written to a .part file and renamed, so an interrupted run cannot leave a
    truncated bill under its final name.
    """
    partial = path.with_name(path.name + '.part')
    with open(partial, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f, delimiter=';', lineterminator='\r\n')
        writer.writerow(HEADER)
        writer.writerows(rows)
    partial.replace(path)


def export(meters: Meters, products: Dict[str, str], conn, period: Period,
           output_dir: Path, force: bool = False) -> Path:
    path = output_dir / f"{period.file_stem}.csv"
    if path.exists() and not force:
        # A file already handed to accounting must not change under it.
        raise FileExistsError(f"{path} already exists; use --force to replace it")
    rows = build_rows(meters, products, fetch_sums(conn, period), period)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(path, rows)
    logger.info(f"Wrote {len(rows)} customer(s) for {period.first_day:%d.%m.%Y}-"
                f"{period.last_day:%d.%m.%Y} to {path}")
    return path


def _required(api_config, section: str, key: str) -> str:
    value = api_config.get(section, {}).get(key)
    if not value:
        raise ValueError(f"{section}.{key} is not configured")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--months', type=int,
                        help='full months before today (default: billing.months)')
    parser.add_argument('--from', dest='first_day', type=date.fromisoformat,
                        help='first day, YYYY-MM-DD, instead of --months')
    parser.add_argument('--to', dest='last_day', type=date.fromisoformat,
                        help='last day, YYYY-MM-DD, with --from')
    parser.add_argument('--output', type=Path, help='directory (default: billing.output_path)')
    parser.add_argument('--force', action='store_true', help='replace an existing file')
    args = parser.parse_args(argv)
    if (args.first_day is None) != (args.last_day is None):
        parser.error('--from and --to go together')
    if args.first_day and args.months:
        parser.error('--months and --from/--to are exclusive')

    configure_logging()
    api_config = load_config('/app/config')
    billing = api_config.get('billing', {})

    if args.first_day:
        if args.last_day < args.first_day:
            parser.error('--to is before --from')
        period = Period(args.first_day, args.last_day)
    else:
        months = args.months or int(billing.get('months', DEFAULT_MONTHS))
        period = last_full_months(datetime.now(LOCAL_TZ).date(), months)

    meters = load_meters(_required(api_config, 'processing', 'meters_file'))
    products = load_products(_required(api_config, 'billing', 'products_file'))
    output_dir = args.output or Path(_required(api_config, 'billing', 'output_path'))

    import psycopg
    dsn = api_config.get('questdb', {}).get('dsn') or QUESTDB_DEFAULT_DSN
    try:
        with psycopg.connect(dsn) as conn:
            export(meters, products, conn, period, output_dir, args.force)
    except FileExistsError as e:
        logger.error(str(e))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
