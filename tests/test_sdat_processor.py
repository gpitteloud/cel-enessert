"""Tests for sdat_processor - the daily batch that ingests a delivery.

What is pinned here is the batch's contract: which files are archived, which are
kept for retry, and where a production breakdown ends up. Attribution itself is a
lookup in the declared meters (see test_meters.py), so nothing below depends on
what else is in the batch -- which is the property the discovery it replaced did
not have.
"""
from pathlib import Path
import zipfile

import pytest

from conftest import SAMPLE_METERS, make_e31_xml, make_e66_xml, meter_id
from scripts.meters import Meters
from scripts.sdat_processor import (FileOutcome, SDATProcessor,
                                    archive_batch_as_zip, group_by_date)

EBIX_TOTAL = '8716867000030'
VSE_CEL = '2404050010123'

PRODUCTION = meter_id('08552310')        # a declared production metering point
CONSUMPTION = meter_id('0046782G')       # the consumption meter it is paired with
MEMBER = meter_id('0020576V')
PRODUCTION_ONLY = meter_id('0134575W')   # declared production-only


def sdat_name(delivery='20260522', time='094500', doc='E66', tag='a1b2c3d4'):
    return f'{delivery}_{time}_12X-0000001536-1_{doc}_12X-00000020FW-5_{tag}.xml'


@pytest.fixture
def processor(tmp_path, fake_questdb):
    """A processor with injected dependencies: no network, no config file."""
    incoming = tmp_path / 'incoming'
    archive = tmp_path / 'archive'
    incoming.mkdir()
    archive.mkdir()
    return SDATProcessor(incoming, archive, fake_questdb.writer, SAMPLE_METERS)


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
    assert p.meters == Meters.empty()


def test_from_config_reads_paths_and_the_declaration(tmp_path):
    meters_file = tmp_path / 'meters.yaml'
    meters_file.write_text(f'production-only:\n  - "{PRODUCTION_ONLY}"\n')
    config = {'questdb': {'dsn': 'postgresql://fake'},
              'processing': {'incoming_path': '/data/incoming',
                             'archive_path': '/data/archive',
                             'meters_file': str(meters_file)}}
    p = SDATProcessor.from_config(config)
    assert p.incoming_dir == Path('/data/incoming')
    assert p.archive_dir == Path('/data/archive')
    assert p.meters.production_only == {PRODUCTION_ONLY}
    assert p.questdb.dsn == 'postgresql://fake'


def test_from_config_refuses_to_run_without_a_declaration():
    """Starting without it would ingest a delivery whose breakdowns fail one by
    one -- 27 files a day, and nothing saying why."""
    config = {'processing': {'incoming_path': '/data/incoming',
                             'archive_path': '/data/archive'}}
    with pytest.raises(ValueError, match='meters_file'):
        SDATProcessor.from_config(config)


def test_from_config_refuses_a_declaration_that_is_not_there(tmp_path):
    config = {'processing': {'incoming_path': '/data/incoming',
                             'archive_path': '/data/archive',
                             'meters_file': str(tmp_path / 'absent.yaml')}}
    with pytest.raises(FileNotFoundError):
        SDATProcessor.from_config(config)


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


def test_paired_production_total_is_skipped_not_failed(processor, fake_questdb):
    """The production metering point repeats its consumption twin's production
    total, so the copy is dropped -- deliberately, and the file is still
    archived."""
    path = drop(processor, make_e66_xml(
        meter_id=PRODUCTION, point='production',
        product_code=EBIX_TOTAL, code_type='ebIXCode'))
    assert processor.process_sdat_file(path) is FileOutcome.SKIPPED
    assert fake_questdb.row_count('cel_energy') == 0


def test_production_breakdown_is_stored_under_the_consumption_meter(processor, fake_questdb):
    """One member is one meter in the database, whichever of their two metering
    points a file came from."""
    path = drop(processor, make_e66_xml(
        meter_id=PRODUCTION, point='production', product_code=VSE_CEL,
        code_type='VSENationalCode', values=(3.0,)))
    assert processor.process_sdat_file(path) is FileOutcome.INGESTED
    stored = {r['meter_id'] for r in fake_questdb.rows['cel_energy'].values()}
    assert stored == {CONSUMPTION}


def test_production_only_meter_keeps_its_own_breakdown(processor, fake_questdb):
    """0134575W has no consumption twin, so its breakdown is its own."""
    path = drop(processor, make_e66_xml(
        meter_id=PRODUCTION_ONLY, point='production', product_code=VSE_CEL,
        code_type='VSENationalCode', values=(3.0,)))
    assert processor.process_sdat_file(path) is FileOutcome.INGESTED
    stored = {r['meter_id'] for r in fake_questdb.rows['cel_energy'].values()}
    assert stored == {PRODUCTION_ONLY}


def test_a_consumption_file_on_a_production_meter_is_skipped(processor, fake_questdb):
    """A provider fault, understood: archived like any other intentional skip, so
    it clears from incoming instead of being re-reported every delivery."""
    path = drop(processor, make_e66_xml(
        meter_id=PRODUCTION_ONLY, point='consumption',
        product_code=EBIX_TOTAL, code_type='ebIXCode'))
    assert processor.process_sdat_file(path) is FileOutcome.SKIPPED
    assert fake_questdb.row_count('cel_energy') == 0


def test_undeclared_meter_fails_rather_than_guessing(processor):
    """A new member appears as an undeclared breakdown file. Failing keeps it in
    incoming for the next delivery instead of storing it on the wrong meter."""
    path = drop(processor, make_e66_xml(
        meter_id=meter_id('09999999'), point='production',
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


def test_an_unreadable_file_is_logged_as_failed(processor, fake_questdb):
    """The gap this closes: a file that never reaches QuestDB was logged nowhere,
    so "which files failed?" could not be answered from the table that claims to."""
    path = drop(processor, 'not xml at all')
    processor.process_sdat_file(path)
    logged = list(fake_questdb.rows['cel_ingest_log'].values())
    assert [(r['outcome'], r['file_name']) for r in logged] == [('failed', path.name)]
    assert fake_questdb.row_count('cel_file_header') == 0, 'nothing was read'


@pytest.mark.parametrize('xml, outcome', [
    (make_e66_xml(meter_id=MEMBER, values=(1.0,)), 'ingested'),
    (make_e66_xml(meter_id=PRODUCTION, point='production',
                  product_code=EBIX_TOTAL, code_type='ebIXCode'), 'skipped'),
    (make_e66_xml(meter_id=MEMBER, values=()), 'failed'),
])
def test_every_readable_file_gets_a_header_row(processor, fake_questdb, xml, outcome):
    """A file that failed is still a file we received, and the header row is what
    says so; the outcome is a separate event."""
    path = drop(processor, xml)
    processor.process_sdat_file(path)
    headers = list(fake_questdb.rows['cel_file_header'].values())
    assert [r['file_name'] for r in headers] == [path.name]
    assert [r['outcome'] for r in fake_questdb.rows['cel_ingest_log'].values()] == [outcome]


def test_the_header_row_keeps_the_files_own_meter(processor, fake_questdb):
    """The rows went to the consumption meter; the file belongs to the production
    metering point."""
    path = drop(processor, make_e66_xml(
        meter_id=PRODUCTION, point='production', product_code=VSE_CEL,
        code_type='VSENationalCode', values=(3.0,)))
    processor.process_sdat_file(path)
    row = list(fake_questdb.rows['cel_file_header'].values())[0]
    assert (row['file_meter_id'],
            row['attributed_meter_id']) == (PRODUCTION, CONSUMPTION)


def test_reprocessing_a_file_leaves_one_header_row_and_two_log_rows(processor, fake_questdb):
    """The two grains: what the file is does not change, what happened does."""
    path = drop(processor, make_e66_xml(meter_id=MEMBER, values=(1.0,)))
    processor.process_sdat_file(path)
    processor.process_sdat_file(path)
    assert fake_questdb.row_count('cel_file_header') == 1
    assert fake_questdb.row_count('cel_ingest_log') == 2


# --------------------------------------------------------------------------
# A whole batch
# --------------------------------------------------------------------------

def test_batch_archives_what_it_handled_and_keeps_what_failed(processor):
    good = drop(processor, make_e66_xml(meter_id=MEMBER, values=(1.0,)), tag='good')
    skipped = drop(processor, make_e66_xml(
        meter_id=PRODUCTION, point='production', product_code=EBIX_TOTAL,
        code_type='ebIXCode'), tag='skip')
    broken = drop(processor, 'not xml', tag='bad')

    processor.process_sdat_files()

    assert not good.exists() and not skipped.exists()
    assert broken.exists(), 'a failed file stays in incoming for the next run'
    with zipfile.ZipFile(processor.archive_dir / '20260522.zip') as zf:
        assert sorted(zf.namelist()) == sorted([good.name, skipped.name])


def test_a_members_two_metering_points_land_on_one_meter(processor, fake_questdb):
    """Both files of a producing member, as a delivery carries them: the
    production metering point's total is dropped as a duplicate, its breakdown
    goes to the consumption meter, and the consumption meter's own total stays."""
    def write(tag, **kwargs):
        drop(processor, make_e66_xml(point='production', **kwargs), tag=tag)

    total = (5.0, 7.0)
    write('v1', meter_id=PRODUCTION, product_code=EBIX_TOTAL,
          code_type='ebIXCode', values=total)
    write('v2', meter_id=PRODUCTION, product_code=VSE_CEL, values=(2.0, 3.0))
    write('p1', meter_id=CONSUMPTION, product_code=EBIX_TOTAL,
          code_type='ebIXCode', values=total)

    processor.process_sdat_files()

    stored = {(r['meter_id'], r['segment'])
              for r in fake_questdb.rows['cel_energy'].values()}
    assert stored == {(CONSUMPTION, 'total'), (CONSUMPTION, 'cel')}


def test_one_file_of_a_delivery_ingests_on_its_own(tmp_path, fake_questdb):
    """The retry case discovery could not serve: value-equality pairing needed the
    consumption meter's total in the same batch, so a production file arriving in
    a later wave -- or left in incoming after a failure -- was unattributable."""
    incoming, archive = tmp_path / 'incoming', tmp_path / 'archive'
    incoming.mkdir()
    archive.mkdir()
    processor = SDATProcessor(incoming, archive, fake_questdb.writer,
                              SAMPLE_METERS)
    (incoming / sdat_name(tag='late')).write_text(
        make_e66_xml(meter_id=PRODUCTION, point='production',
                     product_code=VSE_CEL, values=(2.0, 3.0)), encoding='utf-8')

    processor.process_sdat_files()

    stored = {r['meter_id'] for r in fake_questdb.rows['cel_energy'].values()}
    assert stored == {CONSUMPTION}


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
