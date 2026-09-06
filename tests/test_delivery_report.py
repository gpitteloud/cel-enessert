"""Tests for delivery_report - the end-of-batch, per-observation report.

It replaces validate_daily_balance_sdat, whose one sum per delivery compared a
month of consumption against 5 days of production; the golden tests at the bottom
pin the numbers that module printed, now reported per report period.
"""
import logging
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal

import pytest

from conftest import (SAMPLE_DIR, SAMPLE_MAPPINGS, SAMPLE_SELF_CONTAINED,
                      make_e31_xml, make_e66_xml, meter_id)
from scripts.delivery_report import (attributed_meter, breakdown_checks,
                                     compare_series, e31_slots, e66_slots,
                                     headers_from_files, inventory,
                                     iter_day_files, local_leg, main,
                                     report_delivery, rewritten_keys,
                                     summed_over_meters)
from scripts.discover_meter_mappings import (PeriodResolution,
                                             group_by_report_period,
                                             resolve_periods)
from scripts.sdat_header import parse_header

EBIX_TOTAL = '8716867000030'
VSE_CEL = '2404050010123'
VSE_GRID = '2404050010124'

VIRTUAL = meter_id('08552310')           # in SAMPLE_MAPPINGS
PHYSICAL = meter_id('0046782G')          # its mapped physical meter
MEMBER = meter_id('0020576V')
OTHER = meter_id('0050170B')
SELF_CONTAINED = meter_id('0134575W')    # in SAMPLE_SELF_CONTAINED

START = '2026-05-21T22:00:00Z'
END = '2026-05-26T22:00:00Z'
SLOT = '2026-05-21T22:00:00+00:00'       # the first slot of that interval
NEXT_SLOT = '2026-05-21T22:15:00+00:00'

RESOLVED = PeriodResolution(mappings=dict(SAMPLE_MAPPINGS),
                            self_contained=set(SAMPLE_SELF_CONTAINED))


def sdat_name(delivery='20260527', time='094500', doc='E66', tag='a'):
    return f'{delivery}_{time}_12X-0000001536-1_{doc}_12X-00000020FW-5_{tag}.xml'


def e66(tag='a', **kwargs):
    kwargs.setdefault('start', START)
    kwargs.setdefault('end', END)
    return parse_header(ET.fromstring(make_e66_xml(**kwargs)),
                        sdat_name(tag=tag))


def e31(tag='a', **kwargs):
    kwargs.setdefault('start', START)
    kwargs.setdefault('end', END)
    return parse_header(ET.fromstring(make_e31_xml(**kwargs)),
                        sdat_name(doc='E31', tag=tag))


def attribute(header):
    return attributed_meter(header, RESOLVED.mappings, RESOLVED.self_contained)


# --------------------------------------------------------------------------
# Which meter a file's readings are counted under
# --------------------------------------------------------------------------

def test_a_mapped_virtual_meters_production_total_is_counted_nowhere():
    """It duplicates its physical twin's total, and ingestion drops it -- so
    counting it here would report a gap the database does not have."""
    assert attribute(e66(meter_id=VIRTUAL, point='production',
                         product_code=EBIX_TOTAL, code_type='ebIXCode')) is None


def test_a_breakdown_is_counted_under_the_physical_meter():
    assert attribute(e66(meter_id=VIRTUAL, point='production',
                         product_code=VSE_CEL)) == PHYSICAL


def test_a_self_contained_meter_keeps_its_own_breakdown():
    assert attribute(e66(meter_id=SELF_CONTAINED, point='production',
                         product_code=VSE_CEL)) == SELF_CONTAINED


def test_an_unattributable_breakdown_is_counted_nowhere():
    """Ingestion fails such a file rather than guessing, so it stores no rows."""
    assert attribute(e66(meter_id=meter_id('09999999'), point='production',
                         product_code=VSE_CEL)) is None


def test_rcp_files_are_left_out():
    """RCP is a different domain sharing the folder; E31 does not cover it."""
    rcp = e66(meter_id=MEMBER, business_reason='E88',
              reason_code_type='ebIXCode', community_id=None)
    assert rcp.rcp and attribute(rcp) is None


def test_an_ordinary_consumption_file_is_counted_under_its_own_meter():
    assert attribute(e66(meter_id=MEMBER, point='consumption')) == MEMBER


# --------------------------------------------------------------------------
# Building the two sides
# --------------------------------------------------------------------------

def test_meters_are_summed_slot_by_slot():
    headers = [e66(tag='a', meter_id=MEMBER, values=(1.0, 2.0)),
               e66(tag='b', meter_id=OTHER, values=(0.5, 0.25))]
    e66_side = summed_over_meters(e66_slots(headers, RESOLVED))
    assert e66_side[('consumption', 'cel')] == {SLOT: Decimal('1.5'),
                                                NEXT_SLOT: Decimal('2.25')}


def test_a_virtual_meters_breakdown_lands_on_the_physical_meters_series():
    headers = [e66(tag='a', meter_id=VIRTUAL, point='production',
                   product_code=VSE_CEL, values=(3.0,))]
    assert set(e66_slots(headers, RESOLVED)) == {PHYSICAL}


def test_the_aggregate_side_reads_only_e31():
    headers = [e66(tag='a', meter_id=MEMBER, values=(9.0,)),
               e31(tag='b', flow='E17', values=(4.0,))]
    assert e31_slots(headers) == {('consumption', 'cel'): {SLOT: Decimal('4.0')}}


def test_an_unclassifiable_series_is_left_out_of_both_sides():
    """An unknown flow characteristic has no direction to compare on."""
    assert e31_slots([e31(flow='E20', values=(4.0,))]) == {}


# --------------------------------------------------------------------------
# E31 against the E66 sum, per slot
# --------------------------------------------------------------------------

def test_the_difference_is_signed_and_the_worst_slot_reported():
    """Signed, never absolute: a sign flip means out-of-scope meters leaking into
    the E66 side, which is a different fault from a shortfall."""
    gap, = compare_series(
        {('consumption', 'cel'): {'t1': Decimal('1.000'), 't2': Decimal('2.000')}},
        {('consumption', 'cel'): {'t1': Decimal('1.500'), 't2': Decimal('1.000')}})
    assert (gap.shared, gap.mismatching) == (2, 2)
    assert gap.difference == Decimal('-0.500')
    assert (gap.worst, gap.worst_at) == (Decimal('-1.000'), 't2')


def test_only_slots_present_on_both_sides_are_differenced():
    """The defect the per-observation rewrite removes: a month of E66 against a
    5-day E31 is a grouping artefact, not an imbalance."""
    gap, = compare_series(
        {('production', 'total'): {'t1': Decimal('1.000'), 't2': Decimal('5.000')}},
        {('production', 'total'): {'t1': Decimal('1.000')}})
    assert (gap.shared, gap.mismatching, gap.difference) == (1, 0, Decimal('0'))
    assert (gap.e66_only, gap.e31_only) == (1, 0)


def test_a_reconciling_series_reports_zero_and_no_worst_slot():
    gap, = compare_series({('production', 'total'): {'t1': Decimal('7.000')}},
                          {('production', 'total'): {'t1': Decimal('7.000')}})
    assert (gap.mismatching, gap.difference, gap.worst_at) == (0, Decimal('0'),
                                                              None)


# --------------------------------------------------------------------------
# cel + grid = total, per meter
# --------------------------------------------------------------------------

def breakdown(direction, cel, grid, total):
    return {MEMBER: {(direction, 'cel'): {SLOT: Decimal(cel)},
                     (direction, 'grid'): {SLOT: Decimal(grid)},
                     (direction, 'total'): {SLOT: Decimal(total)}}}


def test_a_meter_whose_legs_add_up_reports_no_mismatch():
    check, = breakdown_checks(breakdown('consumption', '1.000', '2.000', '3.000'))
    assert (check.checked, check.mismatching) == (1, 0)


def test_a_meter_whose_legs_do_not_add_up_is_reported():
    """June's shape: a non-zero total with both breakdowns 0.000, at source."""
    check, = breakdown_checks(breakdown('consumption', '0.000', '0.000', '2.500'))
    assert (check.mismatching, check.worst, check.worst_at) == (1, Decimal('2.500'),
                                                               SLOT)


def test_a_total_only_meter_is_not_checked():
    """Every RCP meter and 0134575W's consumption report a total and no split:
    the identity cannot hold for them by construction."""
    assert breakdown_checks(
        {MEMBER: {('consumption', 'total'): {SLOT: Decimal('2.000')}}}) == []


def test_the_local_leg_needs_no_aggregate():
    """Every kWh drawn from the community was fed in by another member."""
    e66_side = {('consumption', 'cel'): {SLOT: Decimal('5.000')},
                ('production', 'cel'): {SLOT: Decimal('4.500')}}
    assert local_leg(e66_side) == (Decimal('5.000'), Decimal('4.500'))


# --------------------------------------------------------------------------
# Rows one run writes twice
# --------------------------------------------------------------------------

def overlapping(values, end, tag):
    """A file whose first slot coincides with START, in its own report period."""
    return e66(tag=tag, meter_id=MEMBER, values=values, start=START, end=end)


def test_the_same_value_written_twice_is_a_no_op():
    """QuestDB skips a byte-identical row, so the write order does not matter."""
    headers = [overlapping((1.0,), END, 'a'),
               overlapping((1.0,), '2026-06-26T22:00:00Z', 'b')]
    rewrites = rewritten_keys(headers, resolve_periods(headers))
    assert (rewrites.identical, rewrites.differing) == (1, [])


def test_a_revised_value_written_twice_names_both_files():
    """Last-write-wins decides it, and which wave arrives last is not a property
    any code may assume -- so it is reported instead."""
    first = overlapping((1.0,), END, 'a')
    second = overlapping((2.0,), '2026-06-26T22:00:00Z', 'b')
    rewrites = rewritten_keys([first, second],
                              resolve_periods([first, second]))
    assert rewrites.identical == 0
    assert len(rewrites.differing) == 1
    assert first.file_name in rewrites.differing[0]
    assert second.file_name in rewrites.differing[0]


def test_the_two_domains_are_counted_apart():
    rcp = e66(tag='r', meter_id=MEMBER, business_reason='E88',
              reason_code_type='ebIXCode', community_id=None)
    counts = inventory([e66(tag='a', meter_id=MEMBER), e31(tag='b'), rcp])
    assert counts[('CEL', rcp.report_period)] == {'E66': 1, 'E31': 1}
    assert counts[('RCP', rcp.report_period)] == {'E66': 1}


# --------------------------------------------------------------------------
# The report itself
# --------------------------------------------------------------------------

def test_the_report_covers_each_period_separately(caplog):
    monthly = e66(tag='m', meter_id=MEMBER, start='2026-04-30T22:00:00Z',
                  end='2026-05-31T22:00:00Z', values=(1.0,))
    daily = e66(tag='d', meter_id=MEMBER, values=(1.0,))
    with caplog.at_level(logging.INFO):
        report_delivery('20260605', [monthly, daily])
    logged = caplog.text
    assert 'Period 2026-04-30..2026-05-31' in logged
    assert 'Period 2026-05-21..2026-05-26' in logged


def test_the_report_names_the_files_that_were_not_ingested(caplog):
    with caplog.at_level(logging.INFO):
        report_delivery('20260527', [e66(meter_id=MEMBER)],
                        failed=['20260527_094500_broken.xml'])
    assert '20260527_094500_broken.xml' in caplog.text


def test_the_report_survives_a_file_it_cannot_place(caplog):
    """A diagnostic must never fail the ingestion it describes."""
    with caplog.at_level(logging.INFO):
        report_delivery('20260527', [e66(meter_id=meter_id('09999999'),
                                         point='production',
                                         product_code=VSE_CEL)])
    assert 'Delivery report 20260527' in caplog.text


# --------------------------------------------------------------------------
# Finding a delivery's files
# --------------------------------------------------------------------------

def test_loose_files_of_the_delivery_are_read(tmp_path):
    wanted = tmp_path / sdat_name('20260527')
    wanted.write_text('<x/>')
    (tmp_path / sdat_name('20260528')).write_text('<other/>')
    assert list(dict(iter_day_files('20260527', [tmp_path]))) == [wanted.name]


def test_archived_files_are_read_from_the_zip(tmp_path):
    """A delivery already archived must still be reportable, or the report could
    only ever be run on the day of delivery."""
    staged = tmp_path / sdat_name('20260527')
    staged.write_text('<x/>')
    with zipfile.ZipFile(tmp_path / '20260527.zip', 'w') as zf:
        zf.write(staged, arcname=staged.name)
    staged.unlink()
    assert list(dict(iter_day_files('20260527', [tmp_path]))) == [staged.name]


def test_a_file_present_both_loose_and_archived_is_read_once(tmp_path):
    """Mid-run, incoming and the archive overlap; reading both would double one
    meter's contribution and report a fake imbalance."""
    loose = tmp_path / sdat_name('20260527')
    loose.write_text('<loose/>')
    with zipfile.ZipFile(tmp_path / '20260527.zip', 'w') as zf:
        zf.writestr(loose.name, '<archived/>')

    found = list(iter_day_files('20260527', [tmp_path]))
    assert found == [(loose.name, b'<loose/>')], 'the loose copy is read first'


def test_missing_directories_are_skipped(tmp_path):
    assert list(iter_day_files('20260527', [tmp_path / 'nope'])) == []


def test_unreadable_files_are_left_out_rather_than_aborting(caplog):
    headers = headers_from_files([('broken.xml', b'not xml at all'),
                                  (sdat_name(), make_e66_xml().encode())])
    assert [h.file_name for h in headers] == [sdat_name()]


def test_the_cli_reports_a_delivery_it_finds(tmp_path):
    (tmp_path / sdat_name('20260527')).write_text(
        make_e66_xml(meter_id=MEMBER), encoding='utf-8')
    assert main(['20260527', str(tmp_path)]) == 0


def test_the_cli_says_two_when_it_finds_nothing(tmp_path):
    """Not 0: 'nothing to report' must not read as 'the delivery is fine'."""
    assert main(['20260527', str(tmp_path)]) == 2


# --------------------------------------------------------------------------
# Golden deliveries: the numbers validate_daily_balance_sdat printed
# --------------------------------------------------------------------------

def golden_delivery(delivery):
    if not SAMPLE_DIR.is_dir() or not any(SAMPLE_DIR.glob(f'{delivery}_*.xml')):
        pytest.skip(f'real sample files for {delivery} not present')
    return headers_from_files(iter_day_files(delivery, [SAMPLE_DIR]))


def periods_of(headers):
    """{period: (E66 sums, E31 series)} as the report compares them."""
    resolved = resolve_periods(headers)
    return {
        period: (summed_over_meters(e66_slots(group, resolved[period])),
                 e31_slots(group))
        for period, group in group_by_report_period(headers).items()
    }


def test_golden_20260527_reconciles_production_total():
    """One report period, so the delivery's own sums are comparable: the local
    leg is what the old script printed, and production/total lands on 0.000."""
    (e66_side, e31_side), = periods_of(golden_delivery('20260527')).values()
    assert local_leg(e66_side) == (Decimal('408.711'), Decimal('408.913'))
    assert local_leg(e31_side) == (Decimal('408.896'), Decimal('408.896'))

    gaps = {gap.series: gap for gap in compare_series(e66_side, e31_side)}
    total = gaps[('production', 'total')]
    assert (total.shared, total.mismatching) == (480, 0)
    assert total.difference == Decimal('0.000')


def test_golden_20260605_reports_its_two_periods_apart():
    """The defect the rewrite removes: 125.326 vs 76.784 is the 5-day period's
    local leg, not the delivery's -- the monthly wave carries no CEL leg at all,
    so summing the delivery compared a month against 5 days."""
    periods = periods_of(golden_delivery('20260605'))
    legs = {period: local_leg(e66_side)
            for period, (e66_side, _) in periods.items()}
    assert sorted(legs.values()) == [(Decimal('0'), Decimal('0')),
                                     (Decimal('125.326'), Decimal('76.784'))]
