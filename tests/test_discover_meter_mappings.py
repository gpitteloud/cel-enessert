"""Tests for discover_meter_mappings - pairing virtual meters to physical ones.

Written BEFORE the discovery rewrite, so these pin today's behaviour including
the parts that are known defects: classification by the '085' ID prefix, a
0.1 kWh matching tolerance, and first-match-wins pairing. The tests named
`..._today` are the ones the rewrite is expected to change.
"""
import zipfile

import pytest

from conftest import SAMPLE_DIR, make_e66_xml
from scripts import discover_meter_mappings as dmm
from scripts.discover_meter_mappings import (discover_mappings,
                                             get_physical_production_meters,
                                             get_virtual_meters_from_files,
                                             load_or_discover_mappings,
                                             save_mappings)

EBIX_TOTAL = '8716867000030'
VSE_CEL = '2404050010123'

PREFIX = 'CH10111012345000000000000'
PHYSICAL = PREFIX + '0046782G'
VIRTUAL = PREFIX + '08552310'


def total_file(directory, meter_id, values, name):
    """An ebIX production-total document, the input discovery matches on."""
    path = directory / name
    path.write_text(make_e66_xml(meter_id=meter_id, point='production',
                                 product_code=EBIX_TOTAL, code_type='ebIXCode',
                                 values=values), encoding='utf-8')
    return path


def breakdown_file(directory, meter_id, name):
    """A VSE production-breakdown document: what marks a meter as virtual."""
    path = directory / name
    path.write_text(make_e66_xml(meter_id=meter_id, point='production',
                                 product_code=VSE_CEL,
                                 code_type='VSENationalCode'), encoding='utf-8')
    return path


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / 'incoming'
    d.mkdir()
    return d


# --------------------------------------------------------------------------
# Recognising a production total
# --------------------------------------------------------------------------

def test_only_ebix_production_totals_are_considered(data_dir):
    from xml.etree.ElementTree import fromstring
    from scripts.discover_meter_mappings import _production_total_from_root

    def parse(**kwargs):
        return _production_total_from_root(fromstring(make_e66_xml(**kwargs)))

    assert parse(meter_id=PHYSICAL, point='production',
                 product_code=EBIX_TOTAL, code_type='ebIXCode') is not None
    # A consumption point is not a production total.
    assert parse(meter_id=PHYSICAL, point='consumption',
                 product_code=EBIX_TOTAL, code_type='ebIXCode') is None
    # A VSE breakdown is not the total.
    assert parse(meter_id=PHYSICAL, point='production',
                 product_code=VSE_CEL, code_type='VSENationalCode') is None


def test_the_total_is_the_sum_of_the_observations(data_dir):
    from xml.etree.ElementTree import fromstring
    from scripts.discover_meter_mappings import _production_total_from_root

    suffix, total, kind = _production_total_from_root(fromstring(make_e66_xml(
        meter_id=PHYSICAL, point='production', product_code=EBIX_TOTAL,
        code_type='ebIXCode', values=(1.5, 2.25, 3.0))))
    assert (suffix, total, kind) == ('0046782G', 6.75, 'physical')


def test_virtual_is_decided_by_the_085_prefix_today():
    """The rewrite replaces this with a structural test (a virtual meter has no
    consumption file at all), so this assertion is expected to be deleted."""
    from xml.etree.ElementTree import fromstring
    from scripts.discover_meter_mappings import _production_total_from_root

    _, _, kind = _production_total_from_root(fromstring(make_e66_xml(
        meter_id=VIRTUAL, point='production', product_code=EBIX_TOTAL,
        code_type='ebIXCode')))
    assert kind == 'virtual'


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------

def test_equal_totals_are_paired(data_dir):
    total_file(data_dir, PHYSICAL, (1.0, 2.0), '20260522_094500_p.xml')
    total_file(data_dir, VIRTUAL, (1.0, 2.0), '20260522_094500_v.xml')
    assert discover_mappings(data_dir) == {'0046782G': '08552310'}


def test_different_totals_are_not_paired(data_dir):
    total_file(data_dir, PHYSICAL, (1.0, 2.0), '20260522_094500_p.xml')
    total_file(data_dir, VIRTUAL, (9.0, 9.0), '20260522_094500_v.xml')
    assert discover_mappings(data_dir) == {}


def test_totals_within_the_tolerance_are_paired_today(data_dir):
    """A 0.1 kWh tolerance on a float sum. The rewrite compares the whole
    observation vector for exact Decimal equality instead."""
    total_file(data_dir, PHYSICAL, (1.0, 2.0), '20260522_094500_p.xml')
    total_file(data_dir, VIRTUAL, (1.05, 2.0), '20260522_094500_v.xml')
    assert discover_mappings(data_dir) == {'0046782G': '08552310'}


def test_one_virtual_can_be_claimed_by_two_physicals_today(data_dir):
    """A matched virtual is never removed from the pool, so two physical meters
    with the same total both map to it and one of them is wrong. The rewrite
    reports the ambiguity instead of guessing."""
    other = PREFIX + '0020576V'
    total_file(data_dir, PHYSICAL, (1.0,), '20260522_094500_p1.xml')
    total_file(data_dir, other, (1.0,), '20260522_094500_p2.xml')
    total_file(data_dir, VIRTUAL, (1.0,), '20260522_094500_v.xml')

    mappings = discover_mappings(data_dir)
    assert mappings == {'0046782G': '08552310', '0020576V': '08552310'}


def test_totals_are_read_from_the_archive_zip_when_incoming_is_empty(data_dir, tmp_path):
    """Once a delivery is archived, incoming is empty and the ebIX totals exist
    only inside the zip -- discovery has to look there or it finds nothing."""
    archive = tmp_path / 'archive'
    archive.mkdir()
    staging = tmp_path / 'staging'
    staging.mkdir()
    p = total_file(staging, PHYSICAL, (4.0,),
                   '20260522_094500_12X_E66_12X_p.xml')
    v = total_file(staging, VIRTUAL, (4.0,),
                   '20260522_094500_12X_E66_12X_v.xml')
    with zipfile.ZipFile(archive / '20260522.zip', 'w') as zf:
        for f in (p, v):
            zf.write(f, arcname=f.name)

    assert discover_mappings(data_dir, archive) == {'0046782G': '08552310'}


# --------------------------------------------------------------------------
# The physical-producer set
# --------------------------------------------------------------------------

def test_physical_production_meters_collects_ebix_totals(data_dir):
    total_file(data_dir, PHYSICAL, (1.0,), '20260522_094500_p.xml')
    breakdown_file(data_dir, PHYSICAL, '20260522_094500_b.xml')
    assert get_physical_production_meters(data_dir) == {'0046782G'}


def test_physical_production_meters_includes_virtual_meters_today(data_dir):
    """The name is misleading: a virtual meter reports an ebIX total too, so it
    lands in this set. Callers rely on `meter_mappings` to tell them apart."""
    total_file(data_dir, PHYSICAL, (1.0,), '20260522_094500_p.xml')
    total_file(data_dir, VIRTUAL, (1.0,), '20260522_094500_v.xml')
    assert get_physical_production_meters(data_dir) == {'0046782G', '08552310'}


def test_virtual_meters_are_found_from_breakdown_files(data_dir):
    breakdown_file(data_dir, VIRTUAL, '20260522_094500_v.xml')
    breakdown_file(data_dir, PHYSICAL, '20260522_094500_p.xml')
    # Only the 085-prefixed one is reported today.
    assert get_virtual_meters_from_files(data_dir) == {'08552310'}


# --------------------------------------------------------------------------
# The YAML cache
# --------------------------------------------------------------------------

def test_cache_is_used_and_reversed(data_dir, tmp_path, monkeypatch):
    """The cache stores physical -> virtual; callers need virtual -> physical."""
    cache = tmp_path / 'meter_mappings.yaml'
    save_mappings({'0046782G': '08552310'}, cache)
    breakdown_file(data_dir, VIRTUAL, '20260522_094500_v.xml')

    def fail(*a, **k):
        raise AssertionError('discovery must not run when the cache covers the data')
    monkeypatch.setattr(dmm, 'discover_mappings', fail)

    assert load_or_discover_mappings(data_dir, tmp_path, cache) == {
        '08552310': '0046782G'}


def test_a_new_virtual_meter_triggers_rediscovery(data_dir, tmp_path):
    """A new member shows up as a breakdown file for an unknown virtual meter."""
    cache = tmp_path / 'meter_mappings.yaml'
    save_mappings({'0046782G': '08552310'}, cache)

    newcomer = PREFIX + '0857405E'
    breakdown_file(data_dir, newcomer, '20260522_094500_v2.xml')
    total_file(data_dir, newcomer, (7.0,), '20260522_094500_t2.xml')
    total_file(data_dir, PREFIX + '0208254A', (7.0,), '20260522_094500_p2.xml')

    assert load_or_discover_mappings(data_dir, tmp_path, cache) == {
        '0857405E': '0208254A'}


def test_no_cache_discovers_and_writes_one(data_dir, tmp_path):
    cache = tmp_path / 'meter_mappings.yaml'
    total_file(data_dir, PHYSICAL, (2.0,), '20260522_094500_p.xml')
    total_file(data_dir, VIRTUAL, (2.0,), '20260522_094500_v.xml')

    assert load_or_discover_mappings(data_dir, tmp_path, cache) == {
        '08552310': '0046782G'}
    assert cache.exists(), 'a discovered mapping is cached for the next run'


def test_empty_discovery_falls_back_to_the_cache(data_dir, tmp_path, monkeypatch):
    """Discovery finding nothing must not wipe known mappings: without them
    every breakdown file of the delivery would fail."""
    cache = tmp_path / 'meter_mappings.yaml'
    save_mappings({'0046782G': '08552310'}, cache)
    # A breakdown for an unknown meter forces rediscovery, which finds no totals.
    breakdown_file(data_dir, PREFIX + '09999999', '20260522_094500_v.xml')

    assert load_or_discover_mappings(data_dir, tmp_path, cache) == {
        '08552310': '0046782G'}


def test_no_cache_and_no_data_yields_no_mappings(data_dir, tmp_path):
    assert load_or_discover_mappings(
        data_dir, tmp_path, tmp_path / 'absent.yaml') == {}


# --------------------------------------------------------------------------
# Golden test on real files
# --------------------------------------------------------------------------

@pytest.fixture
def real_delivery(tmp_path):
    """A directory of symlinks to one real delivery, or skip if data is absent."""
    files = [f for f in SAMPLE_DIR.glob('20260610_*.xml')
             if f.stat().st_size] if SAMPLE_DIR.is_dir() else []
    if not files:
        pytest.skip('real sample files not present')
    d = tmp_path / '20260610'
    d.mkdir()
    for f in files:
        (d / f.name).symlink_to(f.resolve())
    return d


def test_real_delivery_yields_nine_mappings(real_delivery):
    """The community has 9 virtual meters; every one must pair, or a member's
    production breakdown is dropped for that delivery."""
    mappings = discover_mappings(real_delivery)
    assert len(mappings) == 9
    assert mappings['0046782G'] == '08552310'


def test_real_delivery_includes_the_self_contained_meter(real_delivery):
    """0134575W carries its total and its breakdown on the same ID, so it must
    be in the producer set or its breakdown cannot be attributed to itself."""
    assert '0134575W' in get_physical_production_meters(real_delivery)
