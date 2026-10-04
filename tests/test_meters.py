"""Tests for meters - the provider's declaration, and what it refuses to load.

The customer of every row is a lookup in this file, with no batch context to
fall back on and no check at ingest time to catch a mistake: a meter declared
under the wrong customer files its readings there, and nothing downstream can
tell. So most of what is pinned here is that a bad declaration *raises* rather
than loading a plausible-looking subset.
"""
from pathlib import Path

import pytest

from conftest import meter_id
from scripts.meters import MIN_METER_ID_LENGTH, Meters, load_meters

PRODUCTION = meter_id('08552310')
CONSUMPTION = meter_id('0046782G')
SECOND_CONSUMPTION = meter_id('0050170B')
MEMBER = meter_id('0199054X')

FULL = f"""
customers:
  "9000115":
    consumption:
      - "{CONSUMPTION}"
      - "{SECOND_CONSUMPTION}"
    production:
      - "{PRODUCTION}"
  "9000103":
    consumption:
      - "{MEMBER}"
"""


@pytest.fixture
def declare(tmp_path):
    """Write a declaration and load it, as from_config does at startup."""
    def _declare(text, name='customers.yaml'):
        path = tmp_path / name
        path.write_text(text, encoding='utf-8')
        return load_meters(path)
    return _declare


def customers(body):
    return f'customers:\n{body}'


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def test_every_meter_gets_its_customer_and_role(declare):
    meters = declare(FULL)
    assert meters.owners == {CONSUMPTION: ('9000115', 'consumption'),
                             SECOND_CONSUMPTION: ('9000115', 'consumption'),
                             PRODUCTION: ('9000115', 'production'),
                             MEMBER: ('9000103', 'consumption')}
    assert len(meters) == 4
    assert meters.customers == {'9000115', '9000103'}


def test_lookups(declare):
    meters = declare(FULL)
    assert meters.customer_of(PRODUCTION) == '9000115'
    assert meters.role_of(PRODUCTION) == 'production'
    assert meters.customer_has('9000115', 'production')
    assert not meters.customer_has('9000103', 'production')
    assert meters.meters_of('9000115', 'consumption') == {CONSUMPTION,
                                                         SECOND_CONSUMPTION}


def test_an_undeclared_meter_has_no_customer_and_no_role(declare):
    meters = declare(FULL)
    assert meters.customer_of(meter_id('09999999')) is None
    assert meters.role_of(meter_id('09999999')) is None


def test_a_customer_needs_only_one_role(declare):
    """Most customers only consume."""
    meters = declare(customers(f'  "9000103":\n    consumption: ["{MEMBER}"]\n'))
    assert meters.owners == {MEMBER: ('9000103', 'consumption')}


def test_an_unquoted_customer_id_is_read_as_a_string(declare):
    """YAML reads 9000103 as an int; it must still match the quoted form."""
    meters = declare(customers(f'  9000103:\n    consumption: ["{MEMBER}"]\n'))
    assert meters.customer_of(MEMBER) == '9000103'


def test_ids_are_stripped(declare):
    meters = declare(customers(f'  "9000103":\n    consumption: [" {MEMBER} "]\n'))
    assert meters.role_of(MEMBER) == 'consumption'


def test_an_empty_declaration_declares_nothing():
    """Meters.empty() is for diagnostics and tests; it is never a fallback."""
    meters = Meters.empty()
    assert len(meters) == 0
    assert meters.customer_of(MEMBER) is None


def test_the_real_declaration_loads():
    path = Path(__file__).resolve().parent.parent / 'config' / 'customers.yaml'
    assert len(load_meters(path)) > 0


def test_the_example_declaration_loads():
    path = (Path(__file__).resolve().parent.parent / 'config'
            / 'customers.yaml.example')
    assert len(load_meters(path)) > 0


# --------------------------------------------------------------------------
# What raises
# --------------------------------------------------------------------------

def test_a_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_meters(tmp_path / 'absent.yaml')


def test_an_empty_file_raises(declare):
    with pytest.raises(ValueError, match='no meter'):
        declare('')


def test_a_file_that_is_not_a_mapping_raises(declare):
    with pytest.raises(ValueError, match='mapping'):
        declare(f'- "{MEMBER}"\n')


def test_an_unknown_section_raises(declare):
    """A typo in the root would otherwise read as 'nothing declared'."""
    with pytest.raises(ValueError, match='unknown section'):
        declare(f'customer:\n  "9000103":\n    consumption: ["{MEMBER}"]\n')


def test_customers_that_is_not_a_mapping_raises(declare):
    with pytest.raises(ValueError, match='must map customer ids'):
        declare(f'customers:\n  - "{MEMBER}"\n')


def test_an_unknown_role_raises(declare):
    with pytest.raises(ValueError, match='unknown role'):
        declare(customers(f'  "9000103":\n    consumptions: ["{MEMBER}"]\n'))


def test_a_customer_with_no_meter_raises(declare):
    with pytest.raises(ValueError, match='expected'):
        declare(customers('  "9000103": {}\n'))


def test_an_empty_role_list_raises(declare):
    with pytest.raises(ValueError, match='non-empty list'):
        declare(customers('  "9000103":\n    consumption: []\n'))


def test_a_role_that_is_not_a_list_raises(declare):
    with pytest.raises(ValueError, match='non-empty list'):
        declare(customers(f'  "9000103":\n    consumption: "{MEMBER}"\n'))


def test_a_short_id_raises(declare):
    """The suffix-keyed shape of the deleted mapping cache."""
    with pytest.raises(ValueError, match='not a full meter id'):
        declare(customers('  "9000103":\n    consumption: ["0199054X"]\n'))


def test_a_non_string_id_raises(declare):
    with pytest.raises(ValueError, match='not a full meter id'):
        declare(customers('  "9000103":\n    consumption: [12345]\n'))


def test_the_minimum_length_is_shorter_than_a_real_id():
    assert MIN_METER_ID_LENGTH < len(MEMBER)


def test_an_id_under_two_customers_raises(declare):
    with pytest.raises(ValueError, match='declared twice'):
        declare(customers(f'  "1":\n    consumption: ["{MEMBER}"]\n'
                          f'  "2":\n    consumption: ["{MEMBER}"]\n'))


def test_an_id_in_two_roles_raises(declare):
    with pytest.raises(ValueError, match='declared twice'):
        declare(customers(f'  "1":\n    consumption: ["{MEMBER}"]\n'
                          f'    production: ["{MEMBER}"]\n'))


def test_an_id_repeated_in_one_list_raises(declare):
    with pytest.raises(ValueError, match='declared twice'):
        declare(customers(f'  "1":\n    consumption: ["{MEMBER}", "{MEMBER}"]\n'))
