"""Tests for questdb_writer - the newest delivered value must win.

The contract differs from vm_upsert in one important way. VM keeps the MAXIMUM
value for a duplicated key, so vm_upsert needed a local store, revision
detection, and a whole-series rewrite. QuestDB's DEDUP UPSERT KEYS *replaces* the
row, so a plain INSERT suffices -- but last-write-wins has no notion of "newest
delivery", so **replay order becomes a correctness requirement**. Both halves of
that trade are pinned below, including the regression that out-of-order replay
now causes (documented, not accidental).

`FakeQuestDB` (conftest) mirrors the dedup keys from questdb_schema.sql. If those
keys drift apart, these tests are testing semantics the database does not have --
`test_dedup_keys_match_schema_file` guards exactly that.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import FakeQuestDB, real_files, SAMPLE_MAPPINGS, SAMPLE_PHYSICAL_METERS
from models import MeteredData, MetricType, Observation
import questdb_writer
from questdb_writer import (E31_COLUMNS, E66_COLUMNS, rows_from_e31,
                            rows_from_e66, validate_rows)

TS = '2026-05-22T00:00:00+00:00'
TS2 = '2026-05-22T00:15:00+00:00'


def e66(values, meter_id='CH1011101234500000000000000020576V',
        product_code='8716867000030', metric_type=MetricType.CONSUMPTION_TOTAL,
        condition=None, is_breakdown=False, attributed=None):
    """A parsed E66 document with `values` as consecutive 15-min observations."""
    return MeteredData(
        document_type='E66',
        observations=[
            Observation(sequence=i + 1, timestamp=t, value=Decimal(v),
                        condition=condition)
            for i, (t, v) in enumerate(values)
        ],
        product_code=product_code,
        code_type='ebIXCode',
        community_id='101110-002726',
        metric_type=metric_type,
        meter_id=meter_id,
        metering_point_type='consumption',
        is_production_breakdown=is_breakdown,
        attributed_physical_meter=attributed,
    )


def e31(values, product_code='2404050010123',
        metric_type=MetricType.CONSUMPTION_LOCAL, condition=None):
    return MeteredData(
        document_type='E31',
        observations=[
            Observation(sequence=i + 1, timestamp=t, value=Decimal(v),
                        condition=condition)
            for i, (t, v) in enumerate(values)
        ],
        product_code=product_code,
        code_type='VSENationalCode',
        community_id='101110-002726',
        metric_type=metric_type,
        grid_area='12Y-0000000719-J',
        community_type='CT01',
    )


# --------------------------------------------------------------------------
# Schema agreement
# --------------------------------------------------------------------------

def test_dedup_keys_match_schema_file():
    """The fake's dedup keys must match the real DDL.

    Without this, every LWW assertion below could be validating behaviour the
    database does not implement -- the exact class of mistake that let the VM
    dedup bug hide for so long.
    """
    import re
    from questdb_init import _split_statements

    sql = (Path(__file__).resolve().parent.parent
           / 'scripts' / 'questdb_schema.sql').read_text()

    # Split with the applier's own splitter, not a private regex: it strips
    # comments before splitting on ';', which matters because a '--' comment may
    # legitimately contain a semicolon. Parsing the DDL differently here once
    # made this test read cel_energy as having no dedup at all -- i.e. the guard
    # was wrong about the thing it exists to guard.
    declared = {}
    for statement in _split_statements(sql):
        table = re.search(r'CREATE TABLE IF NOT EXISTS (\w+)', statement)
        if not table:
            continue
        keys = re.search(r'DEDUP UPSERT KEYS\(([^)]*)\)', statement)
        declared[table.group(1)] = (
            tuple(k.strip() for k in keys.group(1).split(',')) if keys else None)

    assert declared == dict(FakeQuestDB.DEDUP_KEYS)


def test_columns_match_schema_file():
    """Writer column lists must exist in the DDL, in the same order."""
    import re
    sql = (Path(__file__).resolve().parent.parent
           / 'scripts' / 'questdb_schema.sql').read_text()

    for table, expected in (('cel_energy', E66_COLUMNS),
                            ('cel_community_energy', E31_COLUMNS)):
        body = re.search(
            rf'CREATE TABLE IF NOT EXISTS {table} \((.*?)\) TIMESTAMP', sql, re.S)
        assert body, f'{table} not found in schema'
        declared = [line.strip().split()[0]
                    for line in body.group(1).split('\n')
                    if line.strip() and not line.strip().startswith('--')]
        assert list(expected) == declared, f'{table} column mismatch'


def test_condition_is_not_a_dedup_key():
    """The regression that would double-count revised slots.

    The provider revises a slot's condition across deliveries. If condition were
    a key, one slot would become two rows and every sum() would count it twice.
    """
    for table, keys in FakeQuestDB.DEDUP_KEYS.items():
        if keys:
            assert 'condition' not in keys, table
            assert 'code_type' not in keys, table


# --------------------------------------------------------------------------
# Row building
# --------------------------------------------------------------------------

def test_rows_from_e66_shape():
    rows = rows_from_e66(e66([(TS, '1.234')]))
    assert len(rows) == 1
    row = dict(zip(E66_COLUMNS, rows[0]))
    assert row['value'] == Decimal('1.234')
    assert row['direction'] == 'consumption' and row['segment'] == 'total'
    assert row['ts'].isoformat() == TS
    assert row['condition'] is None


def test_rows_from_e66_uses_attributed_physical_meter():
    """A production breakdown is stored against the physical meter, matching
    transform_to_datapoints -- otherwise it lands on the virtual twin."""
    virtual = 'CH1011101234500000000000000008552310'
    physical = 'CH1011101234500000000000000046782G'
    rows = rows_from_e66(
        e66([(TS, '1.000')], meter_id=virtual, is_breakdown=True),
        attributed_meter_id=physical)
    assert dict(zip(E66_COLUMNS, rows[0]))['meter_id'] == physical


def test_rows_from_e66_ignores_attribution_when_not_a_breakdown():
    meter = 'CH1011101234500000000000000020576V'
    rows = rows_from_e66(e66([(TS, '1.000')], meter_id=meter),
                         attributed_meter_id='CH999')
    assert dict(zip(E66_COLUMNS, rows[0]))['meter_id'] == meter


def test_rows_from_e31_shape():
    rows = rows_from_e31(e31([(TS, '2.500')]))
    row = dict(zip(E31_COLUMNS, rows[0]))
    assert row['value'] == Decimal('2.500')
    assert row['segment'] == 'cel'
    assert row['grid_area'] == '12Y-0000000719-J'
    assert 'meter_id' not in row


def test_unclassified_metric_type_yields_no_rows():
    doc = e66([(TS, '1.000')])
    doc.metric_type = None
    assert rows_from_e66(doc) == []


def test_missing_observations_yields_no_rows():
    from models import SkippedDocument
    assert rows_from_e66(SkippedDocument(reason='dup')) == []
    assert rows_from_e66(None) == []
    assert rows_from_e31(None) == []


# --------------------------------------------------------------------------
# Exactness
# --------------------------------------------------------------------------

def test_float_value_is_rejected():
    """A float would reintroduce the binary rounding DECIMAL(12,3) prevents."""
    rows = [(TS, 'm', 'consumption', 'total', 'p', 'c', 0.003, 'ebIXCode', None)]
    with pytest.raises(TypeError, match='Decimal'):
        validate_rows(rows, E66_COLUMNS)


def test_decimal_survives_round_trip(fake_questdb):
    """0.002 must stay 0.002 -- the whole reason for the DECIMAL column."""
    fake_questdb.writer.write_e66(e66([(TS, '0.002')]))
    stored = list(fake_questdb.values('cel_energy').values())
    assert stored == [Decimal('0.002')]
    assert str(stored[0]) == '0.002'


def test_exact_sum_of_many_thirds(fake_questdb):
    """A float sum of 0.003 x 1000 drifts; Decimal does not."""
    values = [(f'2026-05-22T{h:02d}:{m:02d}:00+00:00', '0.003')
              for h in range(10) for m in (0, 15, 30, 45)]
    fake_questdb.writer.write_e66(e66(values))
    assert fake_questdb.total('cel_energy') == Decimal('0.120')


def test_row_with_no_value_is_dropped_not_raised():
    rows = [(TS, 'm', 'consumption', 'total', 'p', 'c', None, 'ebIXCode', None),
            (TS2, 'm', 'consumption', 'total', 'p', 'c', Decimal('1'), 'e', None)]
    assert len(validate_rows(rows, E66_COLUMNS)) == 1


# --------------------------------------------------------------------------
# Last-write-wins
# --------------------------------------------------------------------------

def test_new_rows_are_inserted(fake_questdb):
    written = fake_questdb.writer.write_e66(e66([(TS, '1.000'), (TS2, '2.000')]))
    assert written == 2
    assert fake_questdb.row_count('cel_energy') == 2
    assert fake_questdb.committed == 1


def test_downward_revision_wins(fake_questdb):
    """The case VM cannot express: it would keep 0.003."""
    fake_questdb.writer.write_e66(e66([(TS, '0.003')]))
    fake_questdb.writer.write_e66(e66([(TS, '0.002')]))

    assert fake_questdb.row_count('cel_energy') == 1
    assert list(fake_questdb.values('cel_energy').values()) == [Decimal('0.002')]


def test_upward_revision_wins(fake_questdb):
    fake_questdb.writer.write_e66(e66([(TS, '0.002')]))
    fake_questdb.writer.write_e66(e66([(TS, '0.003')]))
    assert list(fake_questdb.values('cel_energy').values()) == [Decimal('0.003')]


def test_repeated_identical_delivery_does_not_duplicate(fake_questdb):
    """The 5-7x overlap must collapse to one row per slot."""
    doc = e66([(TS, '1.000'), (TS2, '2.000')])
    for _ in range(6):
        fake_questdb.writer.write_e66(doc)

    assert fake_questdb.row_count('cel_energy') == 2
    assert fake_questdb.total('cel_energy') == Decimal('3.000')


def test_condition_revision_updates_in_place(fake_questdb):
    """Estimated -> measured must overwrite, never fork into a second row.

    This is the double-count that keeping `condition` out of the dedup key
    prevents, and it is why VM could not store the grade at all.
    """
    fake_questdb.writer.write_e66(e66([(TS, '1.000')], condition='21'))
    fake_questdb.writer.write_e66(e66([(TS, '1.000')], condition=None))

    rows = fake_questdb.rows['cel_energy']
    assert len(rows) == 1
    assert list(rows.values())[0]['condition'] is None
    assert fake_questdb.total('cel_energy') == Decimal('1.000')


def test_different_segments_are_separate_rows(fake_questdb):
    """cel / grid / total share a timestamp and must not collide."""
    fake_questdb.writer.write_e66(
        e66([(TS, '1.000')], metric_type=MetricType.CONSUMPTION_LOCAL,
            product_code='2404050010123'))
    fake_questdb.writer.write_e66(
        e66([(TS, '2.000')], metric_type=MetricType.CONSUMPTION_GRID,
            product_code='2404050010124'))
    assert fake_questdb.row_count('cel_energy') == 2


def test_different_meters_are_separate_rows(fake_questdb):
    fake_questdb.writer.write_e66(e66([(TS, '1.000')], meter_id='CH_A'))
    fake_questdb.writer.write_e66(e66([(TS, '2.000')], meter_id='CH_B'))
    assert fake_questdb.row_count('cel_energy') == 2


def test_e66_and_e31_land_in_separate_tables(fake_questdb):
    """Structural guarantee that sum() cannot mix per-meter with the aggregate."""
    fake_questdb.writer.write_e66(e66([(TS, '1.000')]))
    fake_questdb.writer.write_e31(e31([(TS, '9.000')]))
    assert fake_questdb.row_count('cel_energy') == 1
    assert fake_questdb.row_count('cel_community_energy') == 1


def test_out_of_order_replay_regresses_documented_limitation(fake_questdb):
    """QuestDB has NO staleness guard -- unlike vm_upsert, which refused this.

    Pinned deliberately: replaying an older delivery last overwrites newer data.
    This is why chronological replay order is a correctness requirement (see the
    module docstring and MIGRATION_QUESTDB.md). If QuestDB ever gains a
    conditional upsert, this test should start failing and be replaced.
    """
    fake_questdb.writer.write_e66(e66([(TS, '0.002')]))   # newest delivery
    fake_questdb.writer.write_e66(e66([(TS, '0.003')]))   # stale replay, later

    assert list(fake_questdb.values('cel_energy').values()) == [Decimal('0.003')]


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------

def test_write_failure_rolls_back_and_raises(fake_questdb):
    fake_questdb.fail_next_write = True
    with pytest.raises(RuntimeError, match='injected'):
        fake_questdb.writer.write_e66(e66([(TS, '1.000')]))

    assert fake_questdb.rolled_back == 1
    assert fake_questdb.committed == 0
    assert fake_questdb.row_count('cel_energy') == 0


def test_empty_rows_do_not_open_a_transaction(fake_questdb):
    assert fake_questdb.writer.write_e66(e66([])) == 0
    assert fake_questdb.committed == 0


def test_ingest_log_appends_without_dedup(fake_questdb):
    for _ in range(3):
        fake_questdb.writer.log_ingest('20260527', 'f.xml', 'E66', 96, 'ingested')
    assert fake_questdb.row_count('cel_ingest_log') == 3


def test_ingest_log_failure_is_swallowed(fake_questdb):
    """Provenance must never fail the ingestion it describes."""
    fake_questdb.fail_next_write = True
    fake_questdb.writer.log_ingest('20260527', 'f.xml', 'E66', 0, 'failed')
    assert fake_questdb.row_count('cel_ingest_log') == 0


# --------------------------------------------------------------------------
# A connection the server dropped
# --------------------------------------------------------------------------
#
# Observed in production 2026-08-03: an E31 delivery failed with
#
#   psycopg.OperationalError: consuming input failed: server closed the
#   connection unexpectedly
#     ... during handling of the above exception, another exception occurred:
#   questdb_writer.py:169 in write -> conn.rollback()
#   psycopg.OperationalError: the connection is lost
#
# Two separate defects. (1) This writer holds ONE connection for the life of the
# parser, which runs for weeks and writes in a burst once a day; the socket idles
# ~24h and gets dropped, but psycopg's `closed` only reflects a client-side close
# so _connect() handed the dead connection straight back. (2) The handler's
# unconditional rollback() raised a *second* error on the dead socket, which
# became the headline in the log and buried the actual cause.


class DroppedConnectionError(Exception):
    """Stands in for psycopg.OperationalError, which is not installed here.

    Matched by class NAME (see questdb_writer.CONNECTION_ERROR_NAMES), because
    psycopg is pip-installed in the container and absent in this environment.
    Renamed below so the retry path is reachable in a test.
    """


# Assigned here, not as `__name__ = ...` in the class body: type.__name__ is a
# data descriptor on the metaclass, so it wins over a class-dict entry and the
# in-body form would leave type(exc).__name__ reading 'DroppedConnectionError'.
DroppedConnectionError.__name__ = 'OperationalError'


class FlakyConn:
    """A connection that fails while `budget` has failures left, then works.

    `budget` is a one-element list shared by every connection the fixture opens,
    so poison(2) means "the next two executemany calls fail" regardless of which
    connection serves them. Per-connection counters would make poison(2)
    indistinguishable from poison(1): the retry runs on a *fresh* connection.

    rollback() always raises, reproducing the second exception from the log: the
    real failure mode is that you cannot roll back a connection that is gone.
    """

    def __init__(self, budget, fake):
        self.budget = budget
        self.fake = fake
        self.closed = False
        self.rollbacks_attempted = 0
        self.close_calls = 0

    def cursor(self):
        conn = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def executemany(self, sql, params):
                if conn.budget[0] > 0:
                    conn.budget[0] -= 1
                    raise DroppedConnectionError(
                        'consuming input failed: server closed the connection '
                        'unexpectedly')
                conn.fake.execute(sql, list(params))

        return Cursor()

    def commit(self):
        self.fake.committed += 1

    def rollback(self):
        self.rollbacks_attempted += 1
        raise DroppedConnectionError('the connection is lost')

    def close(self):
        self.close_calls += 1
        self.closed = True


@pytest.fixture
def flaky_questdb(monkeypatch):
    """A writer whose connections drop, tracking how many were opened.

    Unlike `fake_questdb` this patches psycopg.connect-equivalent behaviour, so
    the caching in _connect() is exercised rather than bypassed -- the bug was in
    that caching.
    """
    fake = FakeQuestDB()
    writer = questdb_writer.QuestDBWriter(dsn='postgresql://fake')
    opened = []
    budget = [0]

    def _connect():
        # Mirrors the real _connect(), including the `closed` check that is
        # exactly what fails to notice a server-side drop.
        if writer._conn is None or writer._conn.closed:
            writer._conn = FlakyConn(budget, fake)
            opened.append(writer._conn)
        return writer._conn

    monkeypatch.setattr(writer, '_connect', _connect)
    fake.writer = writer
    fake.opened = opened
    fake.poison = lambda n=1: budget.__setitem__(0, n)
    return fake


def test_dropped_connection_is_replaced_and_the_write_lands(flaky_questdb):
    """The exact production failure: rows must still reach QuestDB."""
    flaky_questdb.poison(1)
    written = flaky_questdb.writer.write_e66(e66([(TS, '1.000')]))

    assert written == 1
    assert flaky_questdb.row_count('cel_energy') == 1
    assert len(flaky_questdb.opened) == 2, 'should have reconnected'
    assert flaky_questdb.committed == 1


def test_the_dead_connection_is_not_reused(flaky_questdb):
    """_discard() must clear the cache; `closed` alone never would.

    Without it the retry re-fetches the same dead connection and fails again --
    and every subsequent delivery keeps failing until the parser restarts, which
    is why one bad night silenced a whole day.
    """
    flaky_questdb.poison(1)
    flaky_questdb.writer.write_e66(e66([(TS, '1.000')]))
    dead = flaky_questdb.opened[0]

    assert dead.close_calls == 1
    assert flaky_questdb.writer._conn is flaky_questdb.opened[1]
    assert flaky_questdb.writer._conn is not dead


def test_failing_rollback_does_not_mask_the_original_error(flaky_questdb):
    """The log must name the cause, not the failed cleanup.

    Both connections drop, so the retry is exhausted and write() raises. The
    exception that escapes must be the server-closed-the-connection one; before
    the fix, rollback()'s "the connection is lost" replaced it.
    """
    flaky_questdb.poison(2)
    with pytest.raises(DroppedConnectionError) as exc:
        flaky_questdb.writer.write_e66(e66([(TS, '1.000')]))

    assert 'server closed the connection' in str(exc.value)
    assert 'the connection is lost' not in str(exc.value)
    assert flaky_questdb.opened[-1].rollbacks_attempted == 1, (
        'rollback should still be attempted, just not allowed to raise')


def test_retry_happens_once_not_forever(flaky_questdb):
    """One retry. A down QuestDB must fail the file, not spin on it."""
    flaky_questdb.poison(5)
    with pytest.raises(DroppedConnectionError):
        flaky_questdb.writer.write_e66(e66([(TS, '1.000')]))

    assert len(flaky_questdb.opened) == 2
    assert flaky_questdb.row_count('cel_energy') == 0


def test_a_query_error_is_not_retried(flaky_questdb):
    """Only connection errors are transient.

    A bad column or a type violation fails identically on retry, so retrying it
    only doubles the log noise and hides that the error is deterministic.
    """
    flaky_questdb.writer.write_e66(e66([(TS, '1.000')]))   # opens conn #1
    flaky_questdb.fail_next_write = True                   # RuntimeError, not OperationalError
    with pytest.raises(RuntimeError, match='injected'):
        flaky_questdb.writer.write_e66(e66([(TS2, '2.000')]))

    assert len(flaky_questdb.opened) == 1, 'must not reconnect'


def test_retried_write_does_not_duplicate_rows(flaky_questdb):
    """The retry is only safe because the INSERT is idempotent.

    If the commit actually landed before the ack was lost, the retry re-sends the
    same rows -- and DEDUP UPSERT KEYS collapses them. Asserting on the resulting
    table rather than on the write count is the point: 2x the inserts, 1x the row.
    """
    flaky_questdb.poison(1)
    flaky_questdb.writer.write_e66(e66([(TS, '1.000'), (TS2, '2.000')]))
    flaky_questdb.writer.write_e66(e66([(TS, '1.000'), (TS2, '2.000')]))

    assert flaky_questdb.row_count('cel_energy') == 2
    assert flaky_questdb.total('cel_energy') == Decimal('3.000')


def test_is_connection_error_classifies_by_name():
    """Guards the name-matching against a rename in questdb_writer."""
    assert questdb_writer.is_connection_error(DroppedConnectionError('x'))
    assert not questdb_writer.is_connection_error(RuntimeError('x'))
    assert not questdb_writer.is_connection_error(TypeError('x'))

    class Subclass(DroppedConnectionError):
        """A driver's subclass must still be recognised, via the MRO."""

    assert questdb_writer.is_connection_error(Subclass('x'))


# --------------------------------------------------------------------------
# Golden test over real overlapping deliveries
# --------------------------------------------------------------------------

def _parse_real(paths):
    """Parse real files into (parsed_doc, attributed_meter_id) pairs."""
    import xml.etree.ElementTree as ET
    from models import SkippedDocument
    from parse_sdat_e66_individual import parse_e66
    from parse_sdat_e31_aggregated import parse_e31

    ns = '{http://www.strom.ch}'
    doc_type_path = f'.//{ns}DocumentType/{ns}ebIXCode'
    out = []
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        elem = root.find(doc_type_path)
        kind = elem.text if elem is not None else None
        if kind == 'E66':
            parsed = parse_e66(root, meter_mappings=SAMPLE_MAPPINGS,
                               physical_production_meters=SAMPLE_PHYSICAL_METERS)
            if parsed is None or isinstance(parsed, SkippedDocument):
                continue
            attributed = None
            if parsed.is_production_breakdown and parsed.attributed_physical_meter:
                virtual = parsed.meter_id or ''
                attributed = virtual[:-8] + parsed.attributed_physical_meter
            out.append((parsed, attributed))
        elif kind == 'E31':
            parsed = parse_e31(root)
            if parsed is not None:
                out.append((parsed, None))
    return out


def _real_deliveries(first='20260527', last='20260603'):
    groups = {}
    for path in real_files('*.xml'):
        delivery = path.name[:8]
        if first <= delivery <= last:
            groups.setdefault(delivery, []).append(path)
    return groups


def test_real_deliveries_end_at_newest_value(fake_questdb):
    """Replaying 8 overlapping real deliveries in order must equal a
    "newest delivery wins" oracle, exactly (no float tolerance)."""
    groups = _real_deliveries()
    if len(groups) < 3:
        pytest.skip('needs >=3 real overlapping deliveries in input/all')

    expected = {}
    for delivery in sorted(groups):
        for parsed, attributed in _parse_real(groups[delivery]):
            if parsed.document_type == 'E31':
                table, columns = 'cel_community_energy', E31_COLUMNS
                rows = rows_from_e31(parsed)
                fake_questdb.writer.write_e31(parsed)
            else:
                table, columns = 'cel_energy', E66_COLUMNS
                rows = rows_from_e66(parsed, attributed)
                fake_questdb.writer.write_e66(parsed, attributed)

            keys = FakeQuestDB.DEDUP_KEYS[table]
            for row in rows:
                record = dict(zip(columns, row))
                expected.setdefault(table, {})[
                    tuple(record[k] for k in keys)] = record['value']

    for table, want in expected.items():
        assert fake_questdb.values(table) == want, table

    # Sanity: the corpus really is dense, not a handful of rows.
    assert fake_questdb.row_count('cel_energy') > 10000


def test_real_deliveries_contain_downward_revisions():
    """Guards the premise: if the corpus stops containing downward revisions,
    the golden test silently stops covering the case VM gets wrong."""
    groups = _real_deliveries()
    if len(groups) < 3:
        pytest.skip('needs >=3 real overlapping deliveries in input/all')

    seen, downward = {}, 0
    for delivery in sorted(groups):
        for parsed, attributed in _parse_real(groups[delivery]):
            rows = (rows_from_e31(parsed) if parsed.document_type == 'E31'
                    else rows_from_e66(parsed, attributed))
            columns = (E31_COLUMNS if parsed.document_type == 'E31'
                       else E66_COLUMNS)
            table = ('cel_community_energy' if parsed.document_type == 'E31'
                     else 'cel_energy')
            keys = FakeQuestDB.DEDUP_KEYS[table]
            for row in rows:
                record = dict(zip(columns, row))
                key = (table,) + tuple(record[k] for k in keys)
                value = record['value']
                if key in seen and value < seen[key]:
                    downward += 1
                seen[key] = value

    assert downward > 0, 'expected the provider to revise some slots downward'
