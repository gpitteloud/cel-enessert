"""Tests for questdb_replay - the archive replay must be ordered and idempotent.

Phase 4 of the migration rebuilds QuestDB's history from the XML archive. Two
properties carry the whole thing:

1. **Chronological order.** DEDUP UPSERT KEYS is last-write-wins with no
   staleness guard, so the newest delivery must be written LAST. If the sort ever
   breaks, older values silently overwrite newer ones -- the exact regression
   vm_upsert used to refuse.
2. **Idempotence.** A replay interrupted halfway must be safe to re-run.

`test_reverse_order_changes_the_result` is the premise guard: it proves ordering
is load-bearing, so the ordering tests are not vacuously true.
"""
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import (FakeQuestDB, SAMPLE_MAPPINGS, SAMPLE_PHYSICAL_METERS,
                      real_files)
from models import SkippedDocument
from parse_sdat import parse_sdat_bytes
from questdb_replay import Replayer, delivery_zips, loose_xml
from questdb_writer import (E31_COLUMNS, E66_COLUMNS, rows_from_e31,
                            rows_from_e66)

XML = (b'<?xml version="1.0" encoding="UTF-8"?>'
       b'<rsm:ValidatedMeteredData_16 xmlns:rsm="http://www.strom.ch">'
       b'</rsm:ValidatedMeteredData_16>')


def _writer(fake):
    """A QuestDBWriter whose connection is `fake`."""
    from questdb_writer import QuestDBWriter

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def executemany(self, sql, params):
            fake.execute(sql, list(params))

    class Conn:
        closed = False

        def cursor(self):
            return Cursor()

        def commit(self):
            fake.committed += 1

        def rollback(self):
            fake.rolled_back += 1

    writer = QuestDBWriter(dsn='postgresql://fake')
    writer._connect = lambda: Conn()
    return writer


# --------------------------------------------------------------------------
# Ordering and discovery (no real data needed)
# --------------------------------------------------------------------------

def _zip(dir_path, name, members=(('a.xml', XML),)):
    path = dir_path / name
    with zipfile.ZipFile(path, 'w') as zf:
        for member, data in members:
            zf.writestr(member, data)
    return path


def test_zips_are_returned_in_chronological_order(tmp_path):
    """Created out of order on purpose: discovery must not depend on mtime."""
    for name in ('20260605.zip', '20260527.zip', '20260603.zip'):
        _zip(tmp_path, name)

    assert [d for d, _ in delivery_zips(tmp_path)] == [
        '20260527', '20260603', '20260605']


def test_non_delivery_zips_are_skipped(tmp_path):
    _zip(tmp_path, '20260527.zip')
    _zip(tmp_path, 'backup.zip')
    _zip(tmp_path, '2026052.zip')      # 7 digits, not a date
    _zip(tmp_path, '20260527_old.zip')

    assert [d for d, _ in delivery_zips(tmp_path)] == ['20260527']


def test_from_and_to_filters(tmp_path):
    for name in ('20260527.zip', '20260603.zip', '20260605.zip'):
        _zip(tmp_path, name)

    assert [d for d, _ in delivery_zips(tmp_path, '20260603', None)] == [
        '20260603', '20260605']
    assert [d for d, _ in delivery_zips(tmp_path, None, '20260603')] == [
        '20260527', '20260603']
    assert [d for d, _ in delivery_zips(tmp_path, '20260603', '20260603')] == [
        '20260603']


def test_loose_xml_is_sorted(tmp_path):
    for name in ('20260605_b.xml', '20260527_a.xml'):
        (tmp_path / name).write_bytes(XML)

    assert [p.name for p in loose_xml(tmp_path)] == [
        '20260527_a.xml', '20260605_b.xml']


def test_empty_zip_members_are_skipped_not_failed(tmp_path):
    """0-byte members are truncated deliveries, not parse failures."""
    path = _zip(tmp_path, '20260527.zip',
                members=(('empty.xml', b''), ('also.xml', b'')))
    replayer = Replayer(_writer(FakeQuestDB()), {}, set())
    assert replayer.replay_zip('20260527', path) == 0
    assert replayer.skipped == 2
    assert replayer.failed == []


def test_unparseable_member_is_recorded_as_failed(tmp_path):
    path = _zip(tmp_path, '20260527.zip',
                members=(('bad.xml', b'<not-xml'),))
    replayer = Replayer(_writer(FakeQuestDB()), {}, set())
    replayer.replay_zip('20260527', path)
    assert replayer.failed == ['bad.xml']


# --------------------------------------------------------------------------
# Golden replay over real archived deliveries
# --------------------------------------------------------------------------

@pytest.fixture(scope='module')
def archive(tmp_path_factory):
    """Real sample files packed into per-delivery zips, as on the NAS."""
    groups = {}
    for path in real_files('*.xml'):
        groups.setdefault(path.name[:8], []).append(path)
    if len(groups) < 3:
        pytest.skip('needs >=3 real overlapping deliveries in input/all')

    # 3 deliveries keeps the run quick while still exercising the overlap.
    out = tmp_path_factory.mktemp('archive')
    for delivery in sorted(groups)[:3]:
        with zipfile.ZipFile(out / f'{delivery}.zip', 'w') as zf:
            for path in groups[delivery]:
                zf.write(path, arcname=path.name)
    return out


def _replay(archive_dir, order=None):
    fake = FakeQuestDB()
    replayer = Replayer(_writer(fake), SAMPLE_MAPPINGS, SAMPLE_PHYSICAL_METERS)
    for delivery, path in (order or delivery_zips(archive_dir)):
        replayer.replay_zip(delivery, path)
    return fake, replayer


def _oracle(archive_dir):
    """Independent 'newest delivery wins' expectation, built without the writer."""
    expected = {}
    for delivery, path in delivery_zips(archive_dir):
        with zipfile.ZipFile(path) as zf:
            for name in sorted(n for n in zf.namelist() if n.endswith('.xml')):
                data = zf.read(name)
                if not data:
                    continue
                parsed = parse_sdat_bytes(
                    data, name, meter_mappings=SAMPLE_MAPPINGS,
                    physical_production_meters=SAMPLE_PHYSICAL_METERS)
                if parsed is None or isinstance(parsed, SkippedDocument):
                    continue
                if parsed.document_type == 'E31':
                    table, columns = 'cel_community_energy', E31_COLUMNS
                    rows = rows_from_e31(parsed)
                else:
                    table, columns = 'cel_energy', E66_COLUMNS
                    attributed = None
                    if (parsed.is_production_breakdown
                            and parsed.attributed_physical_meter):
                        attributed = ((parsed.meter_id or '')[:-8]
                                      + parsed.attributed_physical_meter)
                    rows = rows_from_e66(parsed, attributed)
                keys = FakeQuestDB.DEDUP_KEYS[table]
                for row in rows:
                    record = dict(zip(columns, row))
                    expected.setdefault(table, {})[
                        tuple(record[k] for k in keys)] = record['value']
    return expected


def test_replay_matches_newest_wins_oracle(archive):
    """Exact match, no float tolerance -- values stay Decimal end to end."""
    fake, replayer = _replay(archive)

    expected = _oracle(archive)
    for table, want in expected.items():
        assert fake.values(table) == want, table

    assert replayer.failed == []
    assert fake.row_count('cel_energy') > 10000
    # Overlap really is collapsing: far more rows written than keys kept.
    assert replayer.rows > fake.row_count('cel_energy')


def test_replay_is_idempotent(archive):
    """A re-run after an interruption must not change the result."""
    once, _ = _replay(archive)
    zips = delivery_zips(archive)
    twice, _ = _replay(archive, order=list(zips) + list(zips))

    assert twice.values('cel_energy') == once.values('cel_energy')
    assert twice.row_count('cel_energy') == once.row_count('cel_energy')


def test_reverse_order_changes_the_result(archive):
    """Premise guard: proves the ordering tests above are not vacuous.

    If replaying newest-first produced the same state, DEDUP order would not
    matter and this whole script's sorting would be pointless. It does matter --
    which is why questdb_replay sorts explicitly instead of trusting the caller.
    """
    forward, _ = _replay(archive)
    backward, _ = _replay(archive, order=list(reversed(delivery_zips(archive))))

    assert backward.values('cel_energy') != forward.values('cel_energy')


def test_replayed_values_are_decimal(archive):
    fake, _ = _replay(archive)
    sample = list(fake.values('cel_energy').values())[:200]
    assert sample and all(isinstance(v, Decimal) for v in sample)
