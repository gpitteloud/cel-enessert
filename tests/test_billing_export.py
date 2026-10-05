"""Tests for billing_export - the accounting's import file.

What is pinned: the period (full Zurich months, DST-exact), the 5-block layout
per meter with consumption meters first, the quantities in MWh, and the file's
encoding, since the accounting import reads bytes, not intent.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from conftest import meter_id
from scripts.billing_export import (
    HEADER, Period, build_rows, export, format_mwh, last_full_months, load_products)
from scripts.meters import Meters

PRODUCTS_CSV = (
    "1;Frais de gestion\r\n3001;Electricité CEL injectée\r\n"
    "3002;Électricité CEL soutirée\r\n4001;Gain sur réseau\r\n"
    "4002;Économie sur réseau\r\n5001;Excédent injecté\r\n5002;Consommation réseau\r\n")

PROD = meter_id('0855219K')
CONS_A = meter_id('02291991')
CONS_B = meter_id('01650626')
SOLO = meter_id('0199054X')

# yaml order with production declared first, so the sort is what puts
# consumption first.
METERS = Meters(owners={
    PROD: ('4060519', 'production'),
    CONS_A: ('4060519', 'consumption'),
    CONS_B: ('4060519', 'consumption'),
    SOLO: ('1337266', 'consumption'),
})

PERIOD = Period(date(2026, 7, 1), date(2026, 9, 30))


@pytest.fixture
def products(tmp_path):
    path = tmp_path / 'produits.csv'
    path.write_bytes(PRODUCTS_CSV.encode('cp1252'))
    return load_products(path)


def full(kwh):
    return Decimal(kwh), PERIOD.expected_slots


SUMS = {
    (CONS_A, 'consumption', 'cel'): full('105.4'),
    (CONS_A, 'consumption', 'grid'): full('104'),
    (CONS_B, 'consumption', 'cel'): full('54'),
    (CONS_B, 'consumption', 'grid'): full('205.0004'),
    (PROD, 'production', 'cel'): full('414'),
    (PROD, 'production', 'grid'): full('1610'),
    # the copy of the production total a consumption meter sends is never read
    (CONS_A, 'production', 'cel'): full('999'),
    (SOLO, 'consumption', 'cel'): full('1016'),
    (SOLO, 'consumption', 'grid'): full('1092'),
}


class FakeConn:
    def __init__(self, sums):
        self.rows = [(*key, total, count) for key, (total, count) in sums.items()]
        self.params = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.params = params

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize('today, months, first, last', [
    (date(2026, 10, 4), 3, date(2026, 7, 1), date(2026, 9, 30)),
    (date(2026, 10, 1), 1, date(2026, 9, 1), date(2026, 9, 30)),
    (date(2026, 2, 15), 3, date(2025, 11, 1), date(2026, 1, 31)),
    (date(2026, 3, 31), 1, date(2026, 2, 1), date(2026, 2, 28)),
    (date(2026, 1, 1), 12, date(2025, 1, 1), date(2025, 12, 31)),
])
def test_last_full_months(today, months, first, last):
    assert last_full_months(today, months) == Period(first, last)


def test_period_is_zurich_days_in_utc():
    start, end = PERIOD.utc_bounds
    assert start == datetime(2026, 6, 30, 22, tzinfo=timezone.utc)   # CEST
    assert end == datetime(2026, 9, 30, 22, tzinfo=timezone.utc)


def test_expected_slots_follow_dst():
    # October 2026 has the 25-hour day.
    assert Period(date(2026, 10, 1), date(2026, 10, 31)).expected_slots == 31 * 96 + 4
    assert Period(date(2026, 3, 1), date(2026, 3, 31)).expected_slots == 31 * 96 - 4


@pytest.mark.parametrize('first, last, stem', [
    (date(2026, 7, 1), date(2026, 9, 30), 'import_factures_26T3'),
    (date(2026, 4, 1), date(2026, 6, 30), 'import_factures_26T2'),
    (date(2026, 5, 1), date(2026, 6, 30), 'import_factures_20260501_20260630'),
    (date(2026, 8, 1), date(2026, 10, 31), 'import_factures_20260801_20261031'),
])
def test_file_stem(first, last, stem):
    assert Period(first, last).file_stem == stem


@pytest.mark.parametrize('kwh, text', [
    ('600', '0,6'), ('54', '0,054'), ('205.0004', '0,205'), ('0.5', '0,001'),
    ('0.4', '0'), ('0', '0'), ('6618', '6,618'), ('10000', '10'),
])
def test_format_mwh(kwh, text):
    assert format_mwh(Decimal(kwh)) == text


def test_load_products_requires_every_billed_item(tmp_path):
    path = tmp_path / 'produits.csv'
    path.write_bytes(PRODUCTS_CSV.replace('5002;Consommation réseau\r\n', '')
                     .encode('cp1252'))
    with pytest.raises(ValueError, match='5002'):
        load_products(path)


def test_rows_layout(products):
    rows = build_rows(METERS, products, SUMS, PERIOD)
    assert [r[2] for r in rows] == ['4060519', '1337266']

    first, last, customer, articles, labels, quantities = rows[0]
    assert (first, last) == ('01.07.2026', '30.09.2026')
    assert articles == '|3002|1|4002|5002||3002|1|4002|5002||3001|1|4001|5001'
    assert labels == (
        f'Point de mesure {CONS_A}|Électricité CEL soutirée|Frais de gestion|'
        f'Économie sur réseau|Consommation réseau|'
        f'Point de mesure {CONS_B}|Électricité CEL soutirée|Frais de gestion|'
        f'Économie sur réseau|Consommation réseau|'
        f'Point de mesure {PROD}|Electricité CEL injectée|Frais de gestion|'
        f'Gain sur réseau|Excédent injecté')
    assert quantities == ('|0,105|0,105|0,105|0,104||0,054|0,054|0,054|0,205'
                          '||0,414|0,414|0,414|1,61')
    assert rows[1][5] == '|1,016|1,016|1,016|1,092'


def test_missing_data_bills_zero_and_warns(products, caplog):
    sums = {(SOLO, 'consumption', 'cel'): (Decimal('500'), 10)}
    meters = Meters(owners={SOLO: ('1337266', 'consumption')})
    rows = build_rows(meters, products, sums, PERIOD)
    assert rows[0][5] == '|0,5|0,5|0,5|0'
    warnings = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
    assert any('cel has 10/' in w for w in warnings)
    assert any('grid has 0/' in w for w in warnings)


def test_export_writes_bom_crlf_semicolons(products, tmp_path):
    conn = FakeConn(SUMS)
    path = export(METERS, products, conn, PERIOD, tmp_path / 'out')

    assert path.name == 'import_factures_26T3.csv'
    assert conn.params == PERIOD.utc_bounds
    raw = path.read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')
    lines = raw[3:].decode('utf-8').split('\r\n')
    assert lines[0] == ';'.join(HEADER)
    assert lines[1].startswith('01.07.2026;30.09.2026;4060519;|3002|')
    assert lines[2].startswith('01.07.2026;30.09.2026;1337266;|3002|')
    assert lines[3:] == ['']
    assert '\n' not in raw.decode('utf-8').replace('\r\n', '')
    assert not list(path.parent.glob('*.part'))


def test_export_refuses_to_replace_without_force(products, tmp_path):
    export(METERS, products, FakeConn(SUMS), PERIOD, tmp_path)
    with pytest.raises(FileExistsError):
        export(METERS, products, FakeConn(SUMS), PERIOD, tmp_path)
    export(METERS, products, FakeConn(SUMS), PERIOD, tmp_path, force=True)
