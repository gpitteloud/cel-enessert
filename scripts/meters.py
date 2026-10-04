#!/usr/bin/env python3
"""The community's customers and the metering points they own.

Each metering point is stored under its own id, with the id of the customer that
owns it, so a customer's data is every row carrying that customer id. The
provider's list says which customer owns which metering point and the role of
each one:

    consumption  reports what the site draws: consumption total + cel/grid
    production   reports what it feeds in: production total + cel/grid

A metering point reports only its own direction. The provider sends two kinds
of file that break this rule, and both duplicate or contradict data stored
elsewhere:

- a consumption point whose customer also produces sends a copy of the
  production total, which the production point carries too;
- 0134575W, a production point, gets consumption files, which are a provider
  fault (see PROVIDER_QUESTIONS.md).

Both are total files. So a total whose direction is not its metering point's
role is dropped whenever the customer owns a meter of that direction, and fails
otherwise, because then nothing else carries that data. A CEL or grid breakdown
against the role always fails: a meter measures only its own direction, so it
means the declared role is wrong.

The role is declared rather than derived because a total file cannot show which
meter is its real source, and the breakdown that would tell is often in another
delivery wave (PARSING_GUIDE.md, "Why the role is declared").

The list must be correct before anything is stored, so `load_meters` raises
instead of falling back: a meter filed under the wrong customer cannot be
detected by any later query.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# A VSE national meter id is 33 characters. A short value means the declaration
# was keyed on a suffix -- the shape the deleted mapping cache used -- and half of
# it is not a meter id we can match a file against.
MIN_METER_ID_LENGTH = 20

CUSTOMERS = 'customers'
CONSUMPTION = 'consumption'
PRODUCTION = 'production'
ROLES = (CONSUMPTION, PRODUCTION)


@dataclass(frozen=True)
class Meters:
    """The declared meters: who owns each one, and in which role."""

    # meter id -> (customer id, role)
    owners: Dict[str, Tuple[str, str]]

    @classmethod
    def empty(cls) -> 'Meters':
        """Declares nothing: every meter is then undeclared.

        For a caller with no declaration to hand -- a diagnostic run, a test of
        consumption files -- and deliberately not a fallback the job may take:
        `from_config` loads the real file or raises.
        """
        return cls({})

    def customer_of(self, meter_id: str) -> Optional[str]:
        """The customer owning a meter, or None if it is not declared."""
        owner = self.owners.get(meter_id)
        return owner[0] if owner else None

    def role_of(self, meter_id: str) -> Optional[str]:
        """'consumption' | 'production', or None if the meter is not declared."""
        owner = self.owners.get(meter_id)
        return owner[1] if owner else None

    def customer_has(self, customer_id: str, role: str) -> bool:
        """True if the customer owns at least one meter in that role."""
        return (customer_id, role) in set(self.owners.values())

    def meters_of(self, customer_id: str, role: str) -> FrozenSet[str]:
        """The customer's meters in one role."""
        return frozenset(meter for meter, owner in self.owners.items()
                         if owner == (customer_id, role))

    @property
    def customers(self) -> FrozenSet[str]:
        return frozenset(customer for customer, _ in self.owners.values())

    def __len__(self) -> int:
        """How many metering point ids are declared."""
        return len(self.owners)


def _declared_id(value, where: str) -> str:
    """One id from the file, checked for being a full meter id."""
    if not isinstance(value, str) or len(value.strip()) < MIN_METER_ID_LENGTH:
        raise ValueError(
            f"{where}: {value!r} is not a full meter id (at least "
            f"{MIN_METER_ID_LENGTH} characters)")
    return value.strip()


def _customer_id(value) -> str:
    """A customer id, which YAML reads as an int when it is not quoted."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{CUSTOMERS}: {value!r} is not a customer id")
    customer_id = str(value).strip()
    if not customer_id:
        raise ValueError(f"{CUSTOMERS}: empty customer id")
    return customer_id


def load_meters(path) -> Meters:
    """Read the declared customers and meters, raising on anything untrustworthy.

    Every failure raises: a missing file, an unknown section or role, a short
    id, an id declared twice (even under one customer). The job stops at
    startup instead of storing a delivery whose meters land on the wrong
    customer, which cannot be detected afterwards.
    """
    path = Path(path)
    text = path.read_text(encoding='utf-8')      # FileNotFoundError is the point
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping with a '{CUSTOMERS}' "
                         f"section, got {type(data).__name__}")

    unknown = sorted(str(key) for key in set(data) - {CUSTOMERS})
    if unknown:
        # A typo in the root name would otherwise read as "nothing declared".
        raise ValueError(f"{path}: unknown section(s) {', '.join(unknown)}; "
                         f"expected '{CUSTOMERS}'")

    customers = data.get(CUSTOMERS) or {}
    if not isinstance(customers, dict):
        raise ValueError(f"{path}: '{CUSTOMERS}' must map customer ids to their "
                         f"meters, got {type(customers).__name__}")

    owners: Dict[str, Tuple[str, str]] = {}
    for key, roles in customers.items():
        customer_id = _customer_id(key)
        where = f"{CUSTOMERS}.{customer_id}"
        if not isinstance(roles, dict) or not roles:
            raise ValueError(f"{where}: expected '{CONSUMPTION}' and/or "
                             f"'{PRODUCTION}' lists, got {roles!r}")
        unknown_roles = sorted(str(role) for role in set(roles) - set(ROLES))
        if unknown_roles:
            raise ValueError(f"{where}: unknown role(s) "
                             f"{', '.join(unknown_roles)}; expected "
                             f"{', '.join(ROLES)}")
        for role, ids in roles.items():
            if not isinstance(ids, list) or not ids:
                raise ValueError(f"{where}.{role}: expected a non-empty list of "
                                 f"meter ids, got {ids!r}")
            for value in ids:
                meter_id = _declared_id(value, f"{where}.{role}")
                if meter_id in owners:
                    # One id, one owner and one role: otherwise which one wins
                    # would depend on the order of the file.
                    other_customer, other_role = owners[meter_id]
                    raise ValueError(
                        f"{meter_id} declared twice: {other_customer}.{other_role} "
                        f"and {customer_id}.{role}")
                owners[meter_id] = (customer_id, role)

    meters = Meters(owners=owners)
    if not len(meters):
        raise ValueError(f"{path}: declares no meter at all")

    roles_count = {role: sum(1 for _, r in owners.values() if r == role)
                   for role in ROLES}
    logger.info(f"Declared meters from {path}: {len(meters.customers)} "
                f"customer(s), {roles_count[CONSUMPTION]} consumption and "
                f"{roles_count[PRODUCTION]} production meter(s)")
    return meters
