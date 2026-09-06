"""Tests for sdat_processor - the daily batch that ingests a delivery.

Written BEFORE the ingestion redesign, so they pin what the batch does today:
which files are archived, which are kept for retry, and where a virtual meter's
production breakdown ends up. The attribution assertions are expected to change
shape when the parser stops splicing meter IDs; the archiving and outcome
assertions are not.
"""
from pathlib import Path
import zipfile

import pytest

from conftest import SAMPLE_MAPPINGS, SAMPLE_PHYSICAL_METERS, make_e31_xml, make_e66_xml
from scripts.sdat_processor import (FileOutcome, SDATProcessor,
                                    archive_batch_as_zip, group_by_date)

EBIX_TOTAL = '8716867000030'
VSE_CEL = '2404050010123'

# Full-length IDs share the 25-char prefix; only the last 8 chars differ, which
# is what the current attribution splices.
PREFIX = 'CH10111012345000000000000'
VIRTUAL = PREFIX + '08552310'          # in SAMPLE_MAPPINGS
PHYSICAL = PREFIX + '0046782G'         # its mapped physical meter
MEMBER = PREFIX + '0020576V'
SELF_CONTAINED = PREFIX + '0134575W'   # in SAMPLE_PHYSICAL_METERS


def sdat_name(delivery='20260522', time='094500', doc='E66', tag='a1b2c3d4'):
    return f'{delivery}_{time}_12X-0000001536-1_{doc}_12X-00000020FW-5_{tag}.xml'


@pytest.fixture
def processor(tmp_path, fake_questdb):
    """A processor with injected dependencies: no network, no discovery."""
    incoming = tmp_path / 'incoming'
    archive = tmp_path / 'archive'
    incoming.mkdir()
    archive.mkdir()
    return SDATProcessor(incoming, archive, fake_questdb.writer,
                         meter_mappings=dict(SAMPLE_MAPPINGS),
                         physical_production_meters=set(SAMPLE_PHYSICAL_METERS))


def drop(processor, xml, **name_kwargs):
    """Write an XML document into the incoming folder and return its path."""
    path = processor.incoming_dir / sdat_name(**name_kwargs)
    path.write_text(xml, encoding='utf-8')
    return path


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------

def test_constructor_does_no_io(tmp_path, fake_questdb):
    """Building the object must not scan, parse or connect.

    Discovery used to run in __init__, which made the class untestable and also
    fixed the mappings before the batch was known.
    """
    missing = tmp_path / 'does-not-exist'
    p = SDATProcessor(missing, missing, fake_questdb.writer)
    assert p.meter_mappings is None and p.physical_production_meters is None


def test_from_config_reads_paths_and_builds_a_writer():
    config = {'questdb': {'dsn': 'postgresql://fake'},
              'processing': {'incoming_path': '/data/incoming',
                             'archive_path': '/data/archive',
                             'meter_mapping_file': '/app/config/meter_mappings.yaml'}}
    p = SDATProcessor.from_config(config)
    assert p.incoming_dir == Path('/data/incoming')
    assert p.archive_dir == Path('/data/archive')
    assert p.mapping_cache_file == Path('/app/config/meter_mappings.yaml')
    assert p.questdb.dsn == 'postgresql://fake'


def test_injected_mappings_are_not_rediscovered(processor):
    """An empty mapping is a choice, not a missing value.

    `is None` rather than truthiness, so a test (or a run with no virtual
    meters) cannot silently trigger a filesystem-wide discovery.
    """
    processor.meter_mappings = {}
    processor.physical_production_meters = set()
    processor.resolve_meters()
    assert processor.meter_mappings == {}


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------

def test_group_by_date_groups_on_the_filename_prefix(tmp_path):
    names = [sdat_name('20260605', tag='b'), sdat_name('20260522', tag='c'),
             sdat_name('20260605', tag='a')]
    batches = group_by_date(tmp_path / n for n in names)
    assert sorted(batches) == ['20260522', '20260605']
    assert len(batches['20260605']) == 2


def test_group_by_date_orders_each_batch_by_filename(tmp_path):
    """Filename order is delivery order: the HHMMSS field reproduces the
    provider's send order, and last-write-wins makes that order load-bearing."""
    late = sdat_name('20260807', time='104500', tag='z')
    early = sdat_name('20260807', time='094500', tag='a')
    batches = group_by_date([tmp_path / late, tmp_path / early])
    assert [p.name for p in batches['20260807']] == [early, late]


def test_only_failures_are_kept_out_of_the_archive():
    """An intentional skip is archived; if it were not, incoming would never
    empty and the same ~9 files would be re-reported every delivery."""
    assert FileOutcome.INGESTED.archivable
    assert FileOutcome.SKIPPED.archivable
    assert not FileOutcome.FAILED.archivable


# --------------------------------------------------------------------------
# One file at a time
# --------------------------------------------------------------------------

def test_e66_member_file_is_ingested(processor, fake_questdb):
    path = drop(processor, make_e66_xml(meter_id=MEMBER, values=(1.0, 2.0)))
    assert processor.process_sdat_file(path) is FileOutcome.INGESTED
    assert fake_questdb.row_count('cel_energy') == 2


def test_e31_file_lands_in_the_aggregate_table(processor, fake_questdb):
    path = drop(processor, make_e31_xml(values=(1.0, 2.0)), doc='E31')
    assert processor.process_sdat_file(path) is FileOutcome.INGESTED
    assert fake_questdb.row_count('cel_community_energy') == 2
    assert fake_questdb.row_count('cel_energy') == 0


def test_virtual_meter_production_total_is_skipped_not_failed(processor, fake_questdb):
    """The virtual meter repeats its physical twin's production total, so the
    copy is dropped -- deliberately, and the file is still archived."""
    path = drop(processor, make_e66_xml(
        meter_id=VIRTUAL, point='production',
        product_code=EBIX_TOTAL, code_type='ebIXCode'))
    assert processor.process_sdat_file(path) is FileOutcome.SKIPPED
    assert fake_questdb.row_count('cel_energy') == 0


def test_production_breakdown_is_stored_under_the_physical_meter(processor, fake_questdb):
    """The rows of a virtual meter's breakdown belong to the physical meter."""
    path = drop(processor, make_e66_xml(
        meter_id=VIRTUAL, point='production', product_code=VSE_CEL,
        code_type='VSENationalCode', values=(3.0,)))
    assert processor.process_sdat_file(path) is FileOutcome.INGESTED
    stored = {r['meter_id'] for r in fake_questdb.rows['cel_energy'].values()}
    assert stored == {PHYSICAL}


def test_self_contained_meter_keeps_its_own_breakdown(processor, fake_questdb):
    """0134575W reports the total and the breakdown on the same ID, so its
    breakdown is attributed to itself rather than to a virtual twin."""
    path = drop(processor, make_e66_xml(
        meter_id=SELF_CONTAINED, point='production', product_code=VSE_CEL,
        code_type='VSENationalCode', values=(3.0,)))
    assert processor.process_sdat_file(path) is FileOutcome.INGESTED
    stored = {r['meter_id'] for r in fake_questdb.rows['cel_energy'].values()}
    assert stored == {SELF_CONTAINED}


def test_unknown_virtual_meter_fails_rather_than_guessing(processor):
    """A new member appears as an unmapped breakdown file. Failing keeps it in
    incoming for the next delivery instead of storing it on the wrong meter."""
    path = drop(processor, make_e66_xml(
        meter_id=PREFIX + '09999999', point='production',
        product_code=VSE_CEL, code_type='VSENationalCode'))
    assert processor.process_sdat_file(path) is FileOutcome.FAILED


def test_unparseable_file_fails(processor):
    path = drop(processor, 'not xml at all')
    assert processor.process_sdat_file(path) is FileOutcome.FAILED


def test_file_without_observations_fails(processor):
    """No rows means nothing was stored, so archiving it would lose the day."""
    path = drop(processor, make_e66_xml(meter_id=MEMBER, values=()))
    assert processor.process_sdat_file(path) is FileOutcome.FAILED


def test_write_failure_fails_the_file(processor, fake_questdb):
    """QuestDB is the only store: a failed write must not be archived."""
    path = drop(processor, make_e66_xml(meter_id=MEMBER, values=(1.0,)))
    fake_questdb.fail_next_write = True
    assert processor.process_sdat_file(path) is FileOutcome.FAILED


def test_a_failed_write_is_recorded_in_the_ingest_log(processor, fake_questdb):
    path = drop(processor, make_e66_xml(meter_id=MEMBER, values=(1.0,)))
    fake_questdb.fail_next_write = True
    processor.process_sdat_file(path)
    outcomes = [r['outcome'] for r in fake_questdb.rows['cel_ingest_log'].values()]
    assert outcomes == ['failed']


def test_a_successful_write_is_recorded_in_the_ingest_log(processor, fake_questdb):
    path = drop(processor, make_e66_xml(meter_id=MEMBER, values=(1.0, 2.0)))
    processor.process_sdat_file(path)
    logged = list(fake_questdb.rows['cel_ingest_log'].values())
    assert [r['outcome'] for r in logged] == ['ingested']
    assert logged[0]['rows_written'] == 2
    assert logged[0]['delivery'] == '20260522'
    assert logged[0]['file_name'] == path.name


# --------------------------------------------------------------------------
# A whole batch
# --------------------------------------------------------------------------

def test_batch_archives_what_it_handled_and_keeps_what_failed(processor):
    good = drop(processor, make_e66_xml(meter_id=MEMBER, values=(1.0,)), tag='good')
    skipped = drop(processor, make_e66_xml(
        meter_id=VIRTUAL, point='production', product_code=EBIX_TOTAL,
        code_type='ebIXCode'), tag='skip')
    broken = drop(processor, 'not xml', tag='bad')

    processor.process_sdat_files()

    assert not good.exists() and not skipped.exists()
    assert broken.exists(), 'a failed file stays in incoming for the next run'
    with zipfile.ZipFile(processor.archive_dir / '20260522.zip') as zf:
        assert sorted(zf.namelist()) == sorted([good.name, skipped.name])


def test_each_delivery_date_gets_its_own_zip(processor):
    drop(processor, make_e66_xml(meter_id=MEMBER, values=(1.0,)),
         delivery='20260522', tag='a')
    drop(processor, make_e66_xml(meter_id=MEMBER, values=(1.0,)),
         delivery='20260523', tag='b')
    processor.process_sdat_files()
    zips = sorted(p.name for p in processor.archive_dir.glob('*.zip'))
    assert zips == ['20260522.zip', '20260523.zip']


# --------------------------------------------------------------------------
# Archiving
# --------------------------------------------------------------------------

def test_archive_creates_a_zip_and_removes_the_sources(tmp_path):
    archive = tmp_path / 'archive'
    archive.mkdir()
    files = []
    for i in range(2):
        f = tmp_path / f'20260522_09450{i}_x.xml'
        f.write_text('x')
        files.append(f)

    archive_batch_as_zip(files, '20260522', archive)

    with zipfile.ZipFile(archive / '20260522.zip') as zf:
        assert sorted(zf.namelist()) == sorted(f.name for f in files)
    assert not any(f.exists() for f in files)


def test_archive_appends_to_an_existing_zip(tmp_path):
    """A late wave for a day already archived must be added, not overwrite it --
    this is what lets a delivery be processed in more than one run."""
    archive = tmp_path / 'archive'
    archive.mkdir()
    first = tmp_path / '20260807_094500_a.xml'
    first.write_text('a')
    archive_batch_as_zip([first], '20260807', archive)

    late = tmp_path / '20260807_104500_b.xml'
    late.write_text('b')
    archive_batch_as_zip([late], '20260807', archive)

    with zipfile.ZipFile(archive / '20260807.zip') as zf:
        assert sorted(zf.namelist()) == ['20260807_094500_a.xml',
                                         '20260807_104500_b.xml']


def test_archive_skips_a_name_already_inside_but_still_clears_the_source(tmp_path):
    archive = tmp_path / 'archive'
    archive.mkdir()
    f = tmp_path / '20260522_094500_a.xml'
    f.write_text('first')
    archive_batch_as_zip([f], '20260522', archive)

    f.write_text('second')
    archive_batch_as_zip([f], '20260522', archive)

    with zipfile.ZipFile(archive / '20260522.zip') as zf:
        assert zf.namelist() == ['20260522_094500_a.xml']
        assert zf.read('20260522_094500_a.xml') == b'first'
    assert not f.exists()


def test_archive_failure_keeps_the_sources(tmp_path):
    """If zipping raises, the originals must survive: they are the only copy."""
    f = tmp_path / '20260522_094500_a.xml'
    f.write_text('x')
    archive_batch_as_zip([f], '20260522', tmp_path / 'missing-archive-dir')
    assert f.exists()


def test_archive_ignores_a_file_that_vanished(tmp_path):
    archive = tmp_path / 'archive'
    archive.mkdir()
    present = tmp_path / '20260522_094500_a.xml'
    present.write_text('x')
    gone = tmp_path / '20260522_094500_b.xml'

    archive_batch_as_zip([present, gone], '20260522', archive)

    with zipfile.ZipFile(archive / '20260522.zip') as zf:
        assert zf.namelist() == [present.name]
