#!/usr/bin/env python3
"""The community's meters, as the provider declares them.

Attribution used to be *inferred* from each delivery: a production metering point
reports the same production total as its consumption metering point, value for
value, and that equality paired the two. Inference needs the whole delivery, and
a delivery routinely arrives in waves -- so a late file processed on its own
could not be paired at all. Every branch that handled it existed only because the
linkage is nowhere in the XML: a production file carries its own VSENationalID
and a Community, and nothing else.

The provider declares the list instead, which makes attribution a lookup:

    consumption-only        a member with no production metering point
    consumption-production  a member with both, one id each
    production-only         a production metering point with no consumption

A member who produces has TWO metering point ids: the consumption one, which
reports what the site draws, and the production one, which reports what it feeds
in plus the cel/grid split of it. Both are stored under the consumption id, so
one member is one meter in the database.

`production-only` exists because the provider emits consumption files for a
production metering point that has no consumption at all (0134575W). Those files
are a provider fault and are discarded -- see PROVIDER_QUESTIONS.md.

A declaration error must stop the job before it stores anything, so `load_meters`
raises instead of falling back: a mis-declared pair would file one member's
production under another, and no later query could tell.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Optional

import yaml

logger = logging.getLogger(__name__)

# A VSE national meter id is 33 characters. A short value means the declaration
# was keyed on a suffix -- the shape the deleted mapping cache used -- and half of
# it is not a meter id we can match a file against.
MIN_METER_ID_LENGTH = 20

CONSUMPTION_ONLY = 'consumption-only'
CONSUMPTION_PRODUCTION = 'consumption-production'
PRODUCTION_ONLY = 'production-only'
ROOTS = (CONSUMPTION_ONLY, CONSUMPTION_PRODUCTION, PRODUCTION_ONLY)


@dataclass(frozen=True)
class Meters:
    """The declared meters, as the three lookups attribution needs."""

    # production metering point id -> the consumption metering point of the same
    # member, which is the id its readings are stored under.
    consumption_by_production: Dict[str, str]
    production_only: FrozenSet[str]
    consumption_only: FrozenSet[str]

    @classmethod
    def empty(cls) -> 'Meters':
        """Declares nothing: every production breakdown is then unattributable.

        For a caller with no declaration to hand -- a diagnostic run, a test of
        consumption files -- and deliberately not a fallback the job may take:
        `from_config` loads the real file or raises.
        """
        return cls({}, frozenset(), frozenset())

    def consumption_meter_for(self, meter_id: str) -> Optional[str]:
        """The meter a production breakdown is stored under, or None if undeclared.

        A paired production metering point resolves to its consumption twin; a
        production-only one owns its breakdown and resolves to itself. Anything
        else is a meter we were not told about, and nothing is guessed.
        """
        paired = self.consumption_by_production.get(meter_id)
        if paired:
            return paired
        if meter_id in self.production_only:
            return meter_id
        return None

    def is_paired_production_meter(self, meter_id: str) -> bool:
        """True for a production metering point that has a consumption twin.

        Which is exactly the case where its ebIX production total is a duplicate:
        the twin reports the same total.
        """
        return meter_id in self.consumption_by_production

    def is_production_metering_point(self, meter_id: str) -> bool:
        """True for any declared production metering point.

        A consumption file bearing one of these ids cannot be real -- see
        `production_only` above.
        """
        return (meter_id in self.consumption_by_production
                or meter_id in self.production_only)

    def is_consumption_only(self, meter_id: str) -> bool:
        """True for a member declared as having no production metering point."""
        return meter_id in self.consumption_only

    def __len__(self) -> int:
        """How many metering point ids are declared, over all three roots."""
        return (2 * len(self.consumption_by_production)
                + len(self.production_only) + len(self.consumption_only))


def _declared_id(value, where: str) -> str:
    """One id from the file, checked for being a full meter id."""
    if not isinstance(value, str) or len(value.strip()) < MIN_METER_ID_LENGTH:
        raise ValueError(
            f"{where}: {value!r} is not a full meter id (at least "
            f"{MIN_METER_ID_LENGTH} characters)")
    return value.strip()


def _id_list(data: dict, root: str) -> list:
    """The ids under a list-shaped root."""
    entries = data.get(root) or []
    if not isinstance(entries, list):
        raise ValueError(f"{root}: expected a list of meter ids, got "
                         f"{type(entries).__name__}")
    return [_declared_id(entry, root) for entry in entries]


def _pairs(data: dict) -> Dict[str, str]:
    """{production id: consumption id} from the consumption-production root."""
    entries = data.get(CONSUMPTION_PRODUCTION) or []
    if not isinstance(entries, list):
        raise ValueError(f"{CONSUMPTION_PRODUCTION}: expected a list of "
                         f"consumption/production pairs, got "
                         f"{type(entries).__name__}")

    pairs: Dict[str, str] = {}
    consumption_seen: Dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'consumption',
                                                         'production'}:
            raise ValueError(
                f"{CONSUMPTION_PRODUCTION}: every entry needs exactly a "
                f"'consumption' and a 'production' id, got {entry!r}")
        consumption = _declared_id(entry['consumption'],
                                   f"{CONSUMPTION_PRODUCTION}.consumption")
        production = _declared_id(entry['production'],
                                  f"{CONSUMPTION_PRODUCTION}.production")
        if production == consumption:
            raise ValueError(
                f"{CONSUMPTION_PRODUCTION}: {production} is declared as its own "
                f"consumption meter; a member with one id belongs in "
                f"{PRODUCTION_ONLY}")
        # A repeated id on either side would silently drop or merge a member.
        if production in pairs:
            raise ValueError(f"{CONSUMPTION_PRODUCTION}: production meter "
                             f"{production} is declared twice")
        if consumption in consumption_seen:
            raise ValueError(
                f"{CONSUMPTION_PRODUCTION}: consumption meter {consumption} is "
                f"paired with both {consumption_seen[consumption]} and "
                f"{production}")
        pairs[production] = consumption
        consumption_seen[consumption] = production
    return pairs


def load_meters(path) -> Meters:
    """Read the declared meters, raising on anything it cannot trust.

    Every failure raises: a missing file, an unknown root, a short id, a repeated
    one. The job stops at startup rather than ingesting a delivery whose
    breakdowns land on the wrong member -- which is not detectable afterwards.
    """
    path = Path(path)
    text = path.read_text(encoding='utf-8')      # FileNotFoundError is the point
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of "
                         f"{', '.join(ROOTS)}, got {type(data).__name__}")

    unknown = sorted(set(data) - set(ROOTS))
    if unknown:
        # A typo in a root name would otherwise read as "nothing declared".
        raise ValueError(f"{path}: unknown section(s) {', '.join(unknown)}; "
                         f"expected {', '.join(ROOTS)}")

    pairs = _pairs(data)
    production_only = _id_list(data, PRODUCTION_ONLY)
    consumption_only = _id_list(data, CONSUMPTION_ONLY)

    for root, ids in ((PRODUCTION_ONLY, production_only),
                      (CONSUMPTION_ONLY, consumption_only)):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"{root}: {', '.join(duplicates)} declared twice")

    meters = Meters(consumption_by_production=pairs,
                    production_only=frozenset(production_only),
                    consumption_only=frozenset(consumption_only))
    if not len(meters):
        raise ValueError(f"{path}: declares no meter at all")

    # One id, one role. The same id in two roots makes the lookups disagree, and
    # which one wins would then depend on the order the predicates are called in.
    roles = {CONSUMPTION_PRODUCTION: set(pairs) | set(pairs.values()),
             PRODUCTION_ONLY: set(production_only),
             CONSUMPTION_ONLY: set(consumption_only)}
    for left, right in ((CONSUMPTION_PRODUCTION, PRODUCTION_ONLY),
                        (CONSUMPTION_PRODUCTION, CONSUMPTION_ONLY),
                        (PRODUCTION_ONLY, CONSUMPTION_ONLY)):
        shared = sorted(roles[left] & roles[right])
        if shared:
            raise ValueError(f"{', '.join(shared)} declared in both {left} and "
                             f"{right}")

    logger.info(f"Declared meters from {path}: {len(pairs)} "
                f"consumption/production pair(s), {len(consumption_only)} "
                f"consumption-only, {len(production_only)} production-only")
    return meters
