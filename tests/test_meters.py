"""Tests for meters - the provider's declaration, and what it refuses to load.

Attribution is now a lookup in this file, with no batch context to fall back on
and no equality check to catch a mistake at ingest time: a pair declared wrong
files one member's production under another, and nothing downstream can tell. So
most of what is pinned here is that a bad declaration *raises* rather than
loading a plausible-looking subset.
"""
import pytest

from conftest import meter_id
from scripts.meters import MIN_METER_ID_LENGTH, Meters, load_meters

PRODUCTION = meter_id('08552310')
CONSUMPTION = meter_id('0046782G')
MEMBER = meter_id('0050170B')
PRODUCTION_ONLY = meter_id('0134575W')

FULL = f"""
consumption-only:
  - "{MEMBER}"

consumption-production:
  - consumption: "{CONSUMPTION}"
    production:  "{PRODUCTION}"

production-only:
  - "{PRODUCTION_ONLY}"
"""


@pytest.fixture
def declare(tmp_path):
    """Write a declaration and load it, as from_config does at startup."""
    def _declare(text, name='meters.yaml'):
        path = tmp_path / name
        path.write_text(text, encoding='utf-8')
        return load_meters(path)
    return _declare


# --------------------------------------------------------------------------
# Loading the three roots
# --------------------------------------------------------------------------

def test_all_three_roots_are_read(declare):
    meters = declare(FULL)
    assert meters.consumption_by_production == {PRODUCTION: CONSUMPTION}
    assert meters.production_only == {PRODUCTION_ONLY}
    assert meters.consumption_only == {MEMBER}
    assert len(meters) == 4, 'a pair declares two metering points, not one'


def test_a_root_may_be_absent(declare):
    """A community with no producers is a valid community."""
    meters = declare(f'consumption-only:\n  - "{MEMBER}"\n')
    assert meters.consumption_only == {MEMBER}
    assert meters.consumption_by_production == {}


def test_ids_are_stripped(declare):
    meters = declare(f'production-only:\n  - "  {PRODUCTION_ONLY}  "\n')
    assert meters.production_only == {PRODUCTION_ONLY}


# --------------------------------------------------------------------------
# The lookups attribution makes
# --------------------------------------------------------------------------

def test_a_paired_production_meter_resolves_to_its_twin(declare):
    meters = declare(FULL)
    assert meters.consumption_meter_for(PRODUCTION) == CONSUMPTION
    assert meters.is_paired_production_meter(PRODUCTION)
    assert meters.is_production_metering_point(PRODUCTION)


def test_a_production_only_meter_resolves_to_itself(declare):
    """It has no consumption twin, so its breakdown is its own."""
    meters = declare(FULL)
    assert meters.consumption_meter_for(PRODUCTION_ONLY) == PRODUCTION_ONLY
    assert not meters.is_paired_production_meter(PRODUCTION_ONLY), \
        'its ebIX total is the only one, so it must not be dropped as a duplicate'
    assert meters.is_production_metering_point(PRODUCTION_ONLY)


def test_a_consumption_meter_is_not_a_production_metering_point(declare):
    """The rule that discards a consumption file on a production id must not
    touch the paired consumption meter's own, legitimate consumption files."""
    meters = declare(FULL)
    for consumption in (CONSUMPTION, MEMBER):
        assert not meters.is_production_metering_point(consumption)
        assert meters.consumption_meter_for(consumption) is None


def test_an_undeclared_meter_resolves_to_nothing(declare):
    meters = declare(FULL)
    assert meters.consumption_meter_for(meter_id('09999999')) is None


def test_an_empty_declaration_declares_nothing():
    assert len(Meters.empty()) == 0
    assert Meters.empty().consumption_meter_for(PRODUCTION) is None


# --------------------------------------------------------------------------
# What it refuses to load
# --------------------------------------------------------------------------

def test_a_missing_file_raises(tmp_path):
    """Not an empty declaration: the job must stop before it stores anything."""
    with pytest.raises(FileNotFoundError):
        load_meters(tmp_path / 'absent.yaml')


def test_an_empty_file_raises(declare):
    with pytest.raises(ValueError, match='no meter at all'):
        declare('')


def test_a_file_that_is_not_a_mapping_raises(declare):
    with pytest.raises(ValueError, match='expected a mapping'):
        declare(f'- "{MEMBER}"\n')


def test_an_unknown_root_raises(declare):
    """A typo in a root name would otherwise read as 'nothing declared' -- every
    breakdown then fails one file at a time with nothing saying why."""
    with pytest.raises(ValueError, match='unknown section'):
        declare(f'consumption_only:\n  - "{MEMBER}"\n')


def test_a_root_that_is_not_a_list_raises(declare):
    with pytest.raises(ValueError, match='expected a list'):
        declare(f'consumption-only: "{MEMBER}"\n')


def test_a_pair_root_that_is_not_a_list_raises(declare):
    with pytest.raises(ValueError, match='expected a list'):
        declare(f'consumption-production: "{MEMBER}"\n')


def test_a_short_id_raises(declare):
    """The mapping cache this replaced was keyed on the 8-char suffix, so a
    half-migrated file is the likely mistake -- and would match no file."""
    with pytest.raises(ValueError, match='full meter id'):
        declare('consumption-only:\n  - "0050170B"\n')


def test_a_non_string_id_raises(declare):
    with pytest.raises(ValueError, match='full meter id'):
        declare('consumption-only:\n  - 12345\n')


def test_the_minimum_length_is_shorter_than_a_real_id():
    """The check is a shape guard, not a length assertion: it must pass every
    real 33-char id."""
    assert MIN_METER_ID_LENGTH < len(PRODUCTION)


def test_a_pair_missing_a_key_raises(declare):
    with pytest.raises(ValueError, match="'consumption' and a 'production'"):
        declare(f'consumption-production:\n  - production: "{PRODUCTION}"\n')


def test_a_pair_with_an_extra_key_raises(declare):
    """An unread key means the file says something the code does not honour."""
    with pytest.raises(ValueError, match="'consumption' and a 'production'"):
        declare(f'consumption-production:\n'
                f'  - consumption: "{CONSUMPTION}"\n'
                f'    production:  "{PRODUCTION}"\n'
                f'    note:        "roof"\n')


def test_a_meter_paired_with_itself_raises(declare):
    """A one-id member belongs in production-only; pairing it with itself would
    make its own ebIX total look like a duplicate and drop it."""
    with pytest.raises(ValueError, match='its own consumption meter'):
        declare(f'consumption-production:\n'
                f'  - consumption: "{PRODUCTION}"\n'
                f'    production:  "{PRODUCTION}"\n')


def test_a_production_meter_declared_twice_raises(declare):
    """A dict would keep the last pair and silently drop the first member."""
    with pytest.raises(ValueError, match='declared twice'):
        declare(f'consumption-production:\n'
                f'  - consumption: "{CONSUMPTION}"\n'
                f'    production:  "{PRODUCTION}"\n'
                f'  - consumption: "{MEMBER}"\n'
                f'    production:  "{PRODUCTION}"\n')


def test_one_consumption_meter_in_two_pairs_raises(declare):
    """It would merge two sites' production onto one member."""
    with pytest.raises(ValueError, match='paired with both'):
        declare(f'consumption-production:\n'
                f'  - consumption: "{CONSUMPTION}"\n'
                f'    production:  "{PRODUCTION}"\n'
                f'  - consumption: "{CONSUMPTION}"\n'
                f'    production:  "{PRODUCTION_ONLY}"\n')


def test_a_repeated_id_in_a_list_root_raises(declare):
    with pytest.raises(ValueError, match='declared twice'):
        declare(f'consumption-only:\n  - "{MEMBER}"\n  - "{MEMBER}"\n')


@pytest.mark.parametrize('text, roots', [
    (f'consumption-production:\n'
     f'  - consumption: "{CONSUMPTION}"\n'
     f'    production:  "{PRODUCTION}"\n'
     f'production-only:\n  - "{PRODUCTION}"\n',
     ('consumption-production', 'production-only')),
    (f'consumption-production:\n'
     f'  - consumption: "{CONSUMPTION}"\n'
     f'    production:  "{PRODUCTION}"\n'
     f'consumption-only:\n  - "{CONSUMPTION}"\n',
     ('consumption-production', 'consumption-only')),
    (f'production-only:\n  - "{PRODUCTION_ONLY}"\n'
     f'consumption-only:\n  - "{PRODUCTION_ONLY}"\n',
     ('production-only', 'consumption-only')),
])
def test_an_id_in_two_roots_raises(declare, text, roots):
    """One id, one role. Otherwise which lookup wins depends on the order the
    predicates happen to be called in."""
    with pytest.raises(ValueError) as raised:
        declare(text)
    assert all(root in str(raised.value) for root in roots)
