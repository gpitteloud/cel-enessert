"""Tests for discover_meter_mappings - pairing virtual meters to physical ones.

Two things are pinned here, and they are independent: classification uses only
which series a meter reports (never its ID), and pairing compares the whole
observation vector inside one report period. Anything discovery cannot decide
must come back as an ambiguity -- a wrong pairing stores a member's production
under someone else's meter and cannot be told apart afterwards.
"""
import xml.etree.ElementTree as ET

import pytest

from conftest import SAMPLE_DIR, make_e66_xml, meter_id
from scripts.discover_meter_mappings import (classify_meters, discover_mappings,
                                             group_by_report_period,
                                             load_cached_mappings,
                                             log_mapping_changes,
                                             mappings_for_period,
                                             pair_virtual_meters, save_mappings)
from scripts.sdat_header import load_headers, parse_header

EBIX_TOTAL = '8716867000030'
VSE_CEL = '2404050010123'
VSE_GRID = '2404050010124'

PHYSICAL = meter_id('0046782G')
VIRTUAL = meter_id('08552310')
MEMBER = meter_id('0020576V')

# A second report period, so a monthly file can never be paired with a 5-day one.
MONTH = {'start': '2026-04-30T22:00:00Z', 'end': '2026-05-31T22:00:00Z'}


def header(**kwargs):
    """A FileHeader for a synthetic E66 document."""
    xml = make_e66_xml(**kwargs)
    return parse_header(ET.fromstring(xml), '20260522_094500_x.xml')


def production(meter, product_code=EBIX_TOTAL, values=(1.0, 2.0), **kwargs):
    code_type = 'ebIXCode' if product_code == EBIX_TOTAL else 'VSENationalCode'
    return header(meter_id=meter, point='production', product_code=product_code,
                  code_type=code_type, values=values, **kwargs)


def consumption(meter, product_code=EBIX_TOTAL, **kwargs):
    code_type = 'ebIXCode' if product_code == EBIX_TOTAL else 'VSENationalCode'
    return header(meter_id=meter, point='consumption', product_code=product_code,
                  code_type=code_type, **kwargs)


# --------------------------------------------------------------------------
# Classification, from the series a meter reports
# --------------------------------------------------------------------------

def test_a_meter_with_a_breakdown_and_no_consumption_is_virtual():
    classes = classify_meters([production(VIRTUAL),
                               production(VIRTUAL, VSE_CEL),
                               production(VIRTUAL, VSE_GRID)])
    assert classes.virtual == {VIRTUAL}
    assert classes.self_contained == set()
    # A virtual meter is never its own pairing candidate.
    assert classes.physical_producers == set()


def test_a_meter_with_a_breakdown_and_consumption_is_self_contained():
    """The absence of a consumption file is what makes a meter virtual, so one
    consumption file is enough to say the breakdown is the meter's own."""
    classes = classify_meters([consumption(MEMBER),
                               production(MEMBER),
                               production(MEMBER, VSE_CEL)])
    assert classes.self_contained == {MEMBER}
    assert classes.virtual == set()
    assert classes.physical_producers == {MEMBER}


def test_a_producer_without_a_breakdown_is_a_physical_producer():
    classes = classify_meters([production(PHYSICAL), consumption(PHYSICAL)])
    assert classes.physical_producers == {PHYSICAL}
    assert classes.virtual == set() and classes.self_contained == set()


def test_a_consumer_is_in_no_producer_set():
    classes = classify_meters([consumption(MEMBER),
                               consumption(MEMBER, VSE_CEL),
                               consumption(MEMBER, VSE_GRID)])
    assert (classes.virtual, classes.self_contained,
            classes.physical_producers) == (set(), set(), set())


# --------------------------------------------------------------------------
# Pairing on the whole observation vector
# --------------------------------------------------------------------------

def paired(headers):
    return pair_virtual_meters(headers, classify_meters(headers))


def test_identical_vectors_are_paired():
    headers = [production(VIRTUAL, values=(1.5, 2.25)),
               production(VIRTUAL, VSE_CEL),
               production(PHYSICAL, values=(1.5, 2.25))]
    mappings, ambiguities = paired(headers)
    assert mappings == {VIRTUAL: PHYSICAL}
    assert ambiguities == []


def test_an_all_zero_vector_still_pairs_when_it_is_the_only_one():
    """The real 08552310 reports nothing but zeros and still resolves, because
    no other producer in the group is all-zero."""
    headers = [production(VIRTUAL, values=(0.0, 0.0)),
               production(VIRTUAL, VSE_CEL),
               production(PHYSICAL, values=(0.0, 0.0)),
               production(MEMBER, values=(1.0, 2.0))]
    mappings, ambiguities = paired(headers)
    assert mappings == {VIRTUAL: PHYSICAL}
    assert ambiguities == []


def test_a_vector_that_differs_in_one_slot_is_not_paired():
    """Exact equality, no tolerance: 1.05 is a different meter, not a rounding."""
    headers = [production(VIRTUAL, values=(1.0, 2.0)),
               production(VIRTUAL, VSE_CEL),
               production(PHYSICAL, values=(1.05, 2.0))]
    mappings, ambiguities = paired(headers)
    assert mappings == {}
    assert any('no physical producer' in a for a in ambiguities)


def test_two_matching_producers_are_reported_not_guessed():
    headers = [production(VIRTUAL, values=(1.0,)),
               production(VIRTUAL, VSE_CEL),
               production(PHYSICAL, values=(1.0,)),
               production(MEMBER, values=(1.0,))]
    mappings, ambiguities = paired(headers)
    assert mappings == {}
    assert any(PHYSICAL in a and MEMBER in a for a in ambiguities)


def test_one_physical_meter_cannot_back_two_virtual_meters():
    """Assignment is one-to-one: the second claim is an ambiguity, not an
    overwrite that would silently give one member the other's production."""
    second = meter_id('0855229G')
    headers = [production(VIRTUAL, values=(1.0,)), production(VIRTUAL, VSE_CEL),
               production(second, values=(1.0,)), production(second, VSE_CEL),
               production(PHYSICAL, values=(1.0,))]
    mappings, ambiguities = paired(headers)
    assert len(mappings) == 1
    assert any('matches both' in a for a in ambiguities)


def test_a_virtual_meter_without_a_total_is_reported():
    headers = [production(VIRTUAL, VSE_CEL), production(PHYSICAL)]
    mappings, ambiguities = paired(headers)
    assert mappings == {}
    assert any('no production total' in a for a in ambiguities)


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------

def test_report_periods_are_grouped_separately():
    """A delivery is not one report period: 20260807 carries a month and 5 days,
    whose totals must never be compared with each other."""
    headers = [production(VIRTUAL, values=(1.0,)), production(VIRTUAL, VSE_CEL),
               production(PHYSICAL, values=(1.0,), **MONTH)]
    groups = group_by_report_period(headers)
    assert len(groups) == 2

    results = discover_mappings(headers)
    five_day = results[headers[0].report_period]
    assert five_day.mappings == {}, 'a monthly total is not a candidate'
    assert not five_day.complete


def test_rcp_files_are_left_out():
    """RCP carries only ebIX totals, so it has no virtual meters -- and its
    meters must not become pairing candidates for CEL ones."""
    rcp = production(meter_id('0803097E'), business_reason='E88',
                     reason_code_type='ebIXCode', community_id=None)
    assert rcp.rcp is True
    assert group_by_report_period([rcp]) == {}


def test_a_group_is_complete_only_when_every_virtual_meter_paired():
    headers = [production(VIRTUAL, values=(1.0,)), production(VIRTUAL, VSE_CEL),
               production(PHYSICAL, values=(1.0,))]
    result = discover_mappings(headers)[headers[0].report_period]
    assert result.complete
    assert mappings_for_period(result, cached={'x': 'y'}) == {VIRTUAL: PHYSICAL}


def test_an_incomplete_group_falls_back_to_the_recorded_mappings():
    """Discovery that cannot decide must not wipe what is known: without any
    mapping every breakdown file of the delivery fails."""
    headers = [production(VIRTUAL, values=(1.0,)), production(VIRTUAL, VSE_CEL)]
    result = discover_mappings(headers)[headers[0].report_period]
    assert not result.complete
    assert mappings_for_period(result, cached={VIRTUAL: PHYSICAL}) == {
        VIRTUAL: PHYSICAL}


# --------------------------------------------------------------------------
# The YAML record
# --------------------------------------------------------------------------

def test_saved_mappings_are_read_back(tmp_path):
    cache = tmp_path / 'meter_mappings.yaml'
    save_mappings({VIRTUAL: PHYSICAL}, cache)
    assert load_cached_mappings(cache) == {VIRTUAL: PHYSICAL}


def test_a_suffix_keyed_cache_is_ignored(tmp_path):
    """The previous version keyed on the last 8 characters, which is not a meter
    id. Half-trusting it would attribute a breakdown to nothing."""
    cache = tmp_path / 'meter_mappings.yaml'
    cache.write_text("meter_mappings:\n  '08552310': '0046782G'\n")
    assert load_cached_mappings(cache) == {}


def test_a_missing_or_unreadable_cache_is_empty(tmp_path):
    assert load_cached_mappings(tmp_path / 'absent.yaml') == {}
    broken = tmp_path / 'broken.yaml'
    broken.write_text('meter_mappings: [not, a, mapping]\n')
    assert load_cached_mappings(broken) == {}


def test_a_changed_mapping_is_logged_as_an_error(caplog):
    """A breakdown moving to a different physical meter is either a provider
    change or a mis-pairing; both are worth an error line."""
    with caplog.at_level('INFO'):
        log_mapping_changes({VIRTUAL: PHYSICAL, MEMBER: PHYSICAL},
                            {VIRTUAL: MEMBER})
    changed = [r for r in caplog.records if r.levelname == 'ERROR']
    assert len(changed) == 1 and VIRTUAL in changed[0].message
    assert any('New mapping' in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# Golden test on a real delivery
# --------------------------------------------------------------------------

@pytest.fixture
def real_delivery():
    """Headers of one real delivery, or skip if the sample data is absent."""
    files = [f for f in SAMPLE_DIR.glob('20260610_*.xml')
             if f.stat().st_size] if SAMPLE_DIR.is_dir() else []
    if not files:
        pytest.skip('real sample files not present')
    return load_headers(files)


def test_real_delivery_classifies_without_looking_at_ids(real_delivery):
    """9 virtual meters, 1 self-contained (0134575W), 10 physical producers --
    all from the series each meter reports, with no reference to a prefix."""
    results = discover_mappings(real_delivery.values())
    assert len(results) == 1, 'this delivery is a single report period'
    result = next(iter(results.values()))

    assert len(result.classes.virtual) == 9
    assert result.self_contained == {meter_id('0134575W')}
    assert len(result.classes.physical_producers) == 10


def test_real_delivery_pairs_every_virtual_meter(real_delivery):
    """Every virtual meter must pair, or a member's production breakdown is
    dropped for that delivery."""
    result = next(iter(discover_mappings(real_delivery.values()).values()))
    assert result.ambiguities == []
    assert result.complete
    assert len(result.mappings) == 9
    assert result.mappings[VIRTUAL] == PHYSICAL
