"""Tests for validate_daily_balance_sdat - the CEL local-balance check.

This module is about to be folded into an end-of-batch report that compares
per observation instead of per sum, so these tests exist to make that fold
provably behaviour-preserving: the classification and file-discovery tests
carry over unchanged, and the two golden deliveries pin the numbers it prints
today.
"""
import zipfile

import pytest

from conftest import SAMPLE_DIR, make_e31_xml, make_e66_xml
from scripts.validate_daily_balance_sdat import (classify_e31, classify_e66,
                                                 iter_day_files, validate)

CEL_LOCAL = '2404050010123'
CEL_GRID = '2404050010124'
EBIX_TOTAL = '8716867000030'

PREFIX = 'CH10111012345000000000000'
METER = PREFIX + '0020576V'


def root_of(xml):
    from xml.etree.ElementTree import fromstring
    return fromstring(xml)


def e66(point, product_code=CEL_LOCAL, code_type='VSENationalCode', values=(1.0, 2.0)):
    return root_of(make_e66_xml(meter_id=METER, point=point,
                                product_code=product_code, code_type=code_type,
                                values=values))


def name(delivery='20260527', time='094500', doc='E66', tag='a'):
    return f'{delivery}_{time}_12X-0000001536-1_{doc}_12X-00000020FW-5_{tag}.xml'


# --------------------------------------------------------------------------
# Which files count towards the balance
# --------------------------------------------------------------------------

def test_e66_cel_local_files_are_classified_by_metering_point():
    assert classify_e66(e66('consumption', values=(1.0, 2.5))) == ('consumption', 3.5)
    assert classify_e66(e66('production', values=(4.0,))) == ('production', 4.0)


def test_e66_other_products_are_ignored():
    """Only the CEL-local product balances: the grid leg and the ebIX total are
    other quantities, and adding them would make the check meaningless."""
    assert classify_e66(e66('consumption', product_code=CEL_GRID)) is None
    assert classify_e66(e66('consumption', product_code=EBIX_TOTAL,
                            code_type='ebIXCode')) is None


def test_e31_is_classified_by_flow_characteristic():
    cons = root_of(make_e31_xml(flow='E17', values=(1.0, 2.0)))
    prod = root_of(make_e31_xml(flow='E18', values=(3.0,)))
    assert classify_e31(cons) == ('consumption', 3.0)
    assert classify_e31(prod) == ('production', 3.0)


def test_e31_other_products_and_flows_are_ignored():
    assert classify_e31(root_of(make_e31_xml(product_code=CEL_GRID))) is None
    assert classify_e31(root_of(make_e31_xml(flow='E20'))) is None


# --------------------------------------------------------------------------
# Finding the day's files
# --------------------------------------------------------------------------

def test_loose_files_of_the_day_are_read(tmp_path):
    wanted = tmp_path / name('20260527')
    wanted.write_text('<x/>')
    (tmp_path / name('20260528')).write_text('<other/>')

    found = dict(iter_day_files('20260527', [tmp_path]))
    assert list(found) == [wanted.name]


def test_archived_files_are_read_from_the_zip(tmp_path):
    """A day already archived must still be checkable, or the balance could only
    ever be run on the day of delivery."""
    staged = tmp_path / name('20260527')
    staged.write_text('<x/>')
    with zipfile.ZipFile(tmp_path / '20260527.zip', 'w') as zf:
        zf.write(staged, arcname=staged.name)
    staged.unlink()

    assert list(dict(iter_day_files('20260527', [tmp_path]))) == [staged.name]


def test_a_file_present_both_loose_and_archived_is_counted_once(tmp_path):
    """Mid-run, incoming and the archive overlap; counting twice would double
    one meter's contribution and report a fake imbalance."""
    loose = tmp_path / name('20260527')
    loose.write_text('<loose/>')
    with zipfile.ZipFile(tmp_path / '20260527.zip', 'w') as zf:
        zf.writestr(loose.name, '<archived/>')

    found = list(iter_day_files('20260527', [tmp_path]))
    assert len(found) == 1
    assert found[0][1] == b'<loose/>', 'the loose copy is read first and wins'


def test_missing_directories_are_skipped(tmp_path):
    assert list(iter_day_files('20260527', [tmp_path / 'nope'])) == []


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------

def day(tmp_path, entries):
    """Write (doc_type, xml) pairs into tmp_path under real-shaped names."""
    for i, (doc, xml) in enumerate(entries):
        (tmp_path / name(doc=doc, tag=f'f{i}')).write_text(xml, encoding='utf-8')
    return tmp_path


def test_a_balanced_day_passes(tmp_path, capsys):
    d = day(tmp_path, [
        ('E66', make_e66_xml(meter_id=METER, point='consumption', values=(5.0,))),
        ('E66', make_e66_xml(meter_id=METER, point='production', values=(5.0,))),
        ('E31', make_e31_xml(flow='E17', values=(5.0,))),
        ('E31', make_e31_xml(flow='E18', values=(5.0,))),
    ])
    assert validate('20260527', [d]) == 0
    assert 'PASS' in capsys.readouterr().out


def test_an_imbalanced_day_fails(tmp_path, capsys):
    d = day(tmp_path, [
        ('E66', make_e66_xml(meter_id=METER, point='consumption', values=(100.0,))),
        ('E66', make_e66_xml(meter_id=METER, point='production', values=(20.0,))),
    ])
    assert validate('20260527', [d]) == 1
    assert 'FAIL' in capsys.readouterr().out


def test_a_day_with_no_cel_local_data_returns_two(tmp_path, capsys):
    """Exit 2 is 'nothing to check', which must not be confused with a pass."""
    d = day(tmp_path, [('E66', make_e66_xml(meter_id=METER, point='consumption',
                                            product_code=EBIX_TOTAL,
                                            code_type='ebIXCode'))])
    assert validate('20260527', [d]) == 2
    assert 'No data to validate' in capsys.readouterr().out


def test_unparseable_files_are_counted_and_skipped(tmp_path, capsys):
    d = day(tmp_path, [
        ('E66', make_e66_xml(meter_id=METER, point='consumption', values=(5.0,))),
        ('E66', make_e66_xml(meter_id=METER, point='production', values=(5.0,))),
        ('E66', ''),                      # a 0-byte file, two exist in the corpus
    ])
    assert validate('20260527', [d]) == 0
    assert 'skipped 1 unparseable' in capsys.readouterr().out


# --------------------------------------------------------------------------
# Golden deliveries: the numbers this prints today
# --------------------------------------------------------------------------

def real_day(date_str):
    if not SAMPLE_DIR.is_dir() or not any(SAMPLE_DIR.glob(f'{date_str}_*.xml')):
        pytest.skip(f'real sample files for {date_str} not present')
    return SAMPLE_DIR


def test_golden_20260527_balances(capsys):
    """The delivery that reconciles: one report period, so the sums are
    comparable and every leg lands within tolerance."""
    assert validate('20260527', [real_day('20260527')]) == 0
    out = capsys.readouterr().out
    for expected in ('408.711', '408.913', '408.896'):
        assert expected in out


def test_golden_20260605_fails_because_it_mixes_two_periods(capsys):
    """The defect the per-observation rewrite removes: this delivery carries a
    5-day period AND a whole month, so a single sum compares a month of
    consumption against 5 days of production and reports a false imbalance."""
    assert validate('20260605', [real_day('20260605')]) == 1
    out = capsys.readouterr().out
    assert '125.326' in out and '76.784' in out
