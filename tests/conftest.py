"""Shared pytest fixtures and XML builders for parser tests.

Tests construct SDAT XML in-memory rather than depending on the gitignored
input/ sample files, so they run anywhere.
"""
import sys
from pathlib import Path

import pytest

# Make scripts/ importable
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Directory of real (gitignored) sample files, used by golden-file tests that
# skip when the data is not present (e.g. clean checkout / CI without data).
SAMPLE_DIR = REPO_ROOT / "input" / "all"


def real_files(pattern):
    """Return sorted non-empty real sample files matching pattern, or [] if
    none/absent. Empty (0-byte) files are truncated-delivery errors and are
    excluded - the parser rejects them at runtime; they are not test subjects.
    """
    if not SAMPLE_DIR.is_dir():
        return []
    return sorted(f for f in SAMPLE_DIR.glob(pattern) if f.stat().st_size > 0)


# Real discovered meter mappings for a representative day (virtual -> physical).
# Only needed so production-breakdown E66 files parse instead of returning None.
SAMPLE_MAPPINGS = {
    '0855229G': '0020576V', '08574078': '0217130Y', '08552310': '0046782G',
    '0855227M': '00846565', '0855223Y': '01192538', '08552213': '0125445D',
    '0855219K': '01650626', '0857405E': '0208254A', '0855225S': '0803097E',
}
SAMPLE_PHYSICAL_METERS = {'0134575W'}


RSM_OPEN_E66 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rsm:ValidatedMeteredData_16 xmlns:rsm="http://www.strom.ch">'
)
RSM_OPEN_E31 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rsm:AggregatedMeteredData_13 xmlns:rsm="http://www.strom.ch">'
)


def _observations(values, start_seq=1):
    """Render <Observation> elements. values is a list of floats."""
    parts = []
    for i, v in enumerate(values):
        seq = start_seq + i
        parts.append(
            "<rsm:Observation>"
            f"<rsm:Position><rsm:Sequence>{seq}</rsm:Sequence></rsm:Position>"
            f"<rsm:Volume>{v}</rsm:Volume>"
            "<rsm:Condition>21</rsm:Condition>"
            "</rsm:Observation>"
        )
    return "".join(parts)


def _e66_header(doc_type):
    """ValidatedMeteredData header carrying the DocumentType (used for dispatch)."""
    if doc_type is None:
        return ""
    return (
        '<rsm:ValidatedMeteredData_HeaderInformation>'
        '<rsm:InstanceDocument>'
        f'<rsm:DocumentType><rsm:ebIXCode>{doc_type}</rsm:ebIXCode></rsm:DocumentType>'
        '</rsm:InstanceDocument>'
        '</rsm:ValidatedMeteredData_HeaderInformation>'
    )


def make_e66_xml(
    *,
    doc_type="E66",                # DocumentType/ebIXCode used for dispatch
    meter_id="CH101110123450000000000000020576V",
    point="consumption",          # "consumption" | "production" | None (aggregated)
    product_code="2404050010123",
    code_type="VSENationalCode",   # "VSENationalCode" | "ebIXCode"
    values=(1.0, 2.0, 3.0),
    resolution=15,
    resolution_unit="MIN",
    start="2026-05-21T22:00:00Z",
    end="2026-05-26T22:00:00Z",
    community_id="101110-002726",
    include_interval=True,
    include_resolution=True,
    include_metering_data=True,
):
    """Build a ValidatedMeteredData_1.6 (E66) XML document string."""
    header = _e66_header(doc_type)
    if not include_metering_data:
        return RSM_OPEN_E66 + header + "</rsm:ValidatedMeteredData_16>"

    if point == "consumption":
        mp = (f'<rsm:ConsumptionMeteringPoint>'
              f'<rsm:VSENationalID>{meter_id}</rsm:VSENationalID>'
              f'</rsm:ConsumptionMeteringPoint>')
    elif point == "production":
        mp = (f'<rsm:ProductionMeteringPoint>'
              f'<rsm:VSENationalID>{meter_id}</rsm:VSENationalID>'
              f'</rsm:ProductionMeteringPoint>')
    else:
        mp = ""  # aggregated / no metering point

    interval = ""
    if include_interval:
        interval = (f'<rsm:Interval><rsm:StartDateTime>{start}</rsm:StartDateTime>'
                    f'<rsm:EndDateTime>{end}</rsm:EndDateTime></rsm:Interval>')

    res = ""
    if include_resolution:
        res = (f'<rsm:Resolution><rsm:Resolution>{resolution}</rsm:Resolution>'
               f'<rsm:Unit>{resolution_unit}</rsm:Unit></rsm:Resolution>')

    product = ""
    if product_code:
        product = (f'<rsm:Product><rsm:ID><rsm:{code_type}>{product_code}'
                   f'</rsm:{code_type}></rsm:ID>'
                   f'<rsm:MeasureUnit>KWH</rsm:MeasureUnit></rsm:Product>')

    community = ""
    if community_id:
        community = (f'<rsm:Community><rsm:CommunityID>{community_id}'
                     f'</rsm:CommunityID></rsm:Community>')

    return (
        RSM_OPEN_E66
        + header
        + "<rsm:MeteringData>"
        + interval + res + mp + product + community
        + _observations(list(values))
        + "</rsm:MeteringData></rsm:ValidatedMeteredData_16>"
    )


def make_e31_xml(
    *,
    doc_type="E31",
    product_code="2404050010123",
    code_type="VSENationalCode",   # E31 real files use VSENationalCode
    flow="E17",                    # E17 consumption, E18 production
    values=(1.0, 2.0, 3.0),
    resolution=15,
    start="2026-06-10T22:00:00Z",
    end="2026-06-15T22:00:00Z",
    community_id="101110-002726",
    community_type="CT01",
    grid_area="12Y-0000000719-J",
    include_metering_data=True,
    include_start=True,
):
    """Build an AggregatedMeteredData_1.3 (E31) XML document string."""
    doc_type_elem = (
        f'<rsm:DocumentType><rsm:ebIXCode>{doc_type}</rsm:ebIXCode></rsm:DocumentType>'
        if doc_type is not None else ''
    )
    header = (
        '<rsm:AggregatedMeteredData_HeaderInformation>'
        '<rsm:BusinessScopeProcess>'
        '<rsm:BusinessReasonType codeListID="VSE">'
        '<rsm:VSENationalCode>C40</rsm:VSENationalCode>'
        '</rsm:BusinessReasonType>'
        '</rsm:BusinessScopeProcess>'
        '<rsm:InstanceDocument>'
        f'{doc_type_elem}'
        '</rsm:InstanceDocument>'
        '</rsm:AggregatedMeteredData_HeaderInformation>'
    )

    if not include_metering_data:
        return RSM_OPEN_E31 + header + "</rsm:AggregatedMeteredData_13>"

    interval = "<rsm:Interval>"
    if include_start:
        interval += f"<rsm:StartDateTime>{start}</rsm:StartDateTime>"
    interval += f"<rsm:EndDateTime>{end}</rsm:EndDateTime></rsm:Interval>"

    res = (f'<rsm:Resolution><rsm:Resolution>{resolution}</rsm:Resolution>'
           f'<rsm:Unit>MIN</rsm:Unit></rsm:Resolution>')
    grid = (f'<rsm:MeteringGridArea><rsm:EICID>{grid_area}</rsm:EICID>'
            f'</rsm:MeteringGridArea>')
    product = ""
    if product_code:
        product = (f'<rsm:Product><rsm:ID><rsm:{code_type}>{product_code}'
                   f'</rsm:{code_type}></rsm:ID><rsm:MeasureUnit>KWH</rsm:MeasureUnit>'
                   f'</rsm:Product>')
    agg = ""
    if flow:
        agg = (f'<rsm:AggregationCriteria><rsm:FlowCharacteristic>{flow}'
               f'</rsm:FlowCharacteristic>'
               f'<rsm:SettlementMethodCharacteristic>E02'
               f'</rsm:SettlementMethodCharacteristic></rsm:AggregationCriteria>')
    community = ""
    if community_id:
        community = (f'<rsm:Community><rsm:CommunityID>{community_id}</rsm:CommunityID>'
                     f'<rsm:CommunityType><rsm:VSENationalCode>{community_type}'
                     f'</rsm:VSENationalCode></rsm:CommunityType></rsm:Community>')

    return (
        RSM_OPEN_E31 + header
        + "<rsm:MeteringData>"
        + interval + res + grid + product + agg + community
        + _observations(list(values))
        + "</rsm:MeteringData></rsm:AggregatedMeteredData_13>"
    )


@pytest.fixture
def write_xml(tmp_path):
    """Return a helper that writes XML text to a temp file and returns its Path."""
    def _write(xml_text, name="doc.xml"):
        p = tmp_path / name
        p.write_text(xml_text, encoding="utf-8")
        return p
    return _write


# --------------------------------------------------------------------------
# Fake QuestDB
# --------------------------------------------------------------------------

class FakeQuestDB:
    """In-memory stand-in reproducing DEDUP UPSERT KEYS: last write wins.

    Last-write-wins is what makes the provider's downward revisions land: a
    re-delivered slot replaces the stored row instead of being merged with it.

    ``rows`` is ``{table: {key_tuple: full_row}}``, so a key collision overwrites
    exactly as the database would. ``inserted`` counts rows sent (including ones
    that overwrote), letting tests assert on write volume.
    """

    # Must mirror questdb_schema.sql. A drift here would make the tests pass
    # against semantics the database does not actually have.
    DEDUP_KEYS = {
        'cel_energy': ('ts', 'meter_id', 'direction', 'segment', 'product_code',
                       'community_id'),
        'cel_community_energy': ('ts', 'direction', 'segment', 'product_code',
                                 'community_id'),
        'cel_ingest_log': None,       # no dedup: every ingestion is an event
    }

    def __init__(self):
        self.rows = {}
        self.inserted = 0
        self.statements = []
        self.fail_next_write = False
        self.committed = 0
        self.rolled_back = 0

    def execute(self, sql, params):
        """Apply one INSERT the way QuestDB would."""
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError('injected QuestDB write failure')

        self.statements.append(sql)
        table, columns = self._parse_insert(sql)
        table_rows = self.rows.setdefault(table, {})
        keys = self.DEDUP_KEYS.get(table)

        for row in params:
            record = dict(zip(columns, row))
            if keys is None:
                # No dedup: append under a synthetic unique key.
                table_rows[len(table_rows)] = record
            else:
                table_rows[tuple(record[k] for k in keys)] = record
            self.inserted += 1

    @staticmethod
    def _parse_insert(sql):
        import re as _re
        match = _re.match(
            r'INSERT INTO (\w+) \(([^)]+)\) VALUES', sql.strip())
        assert match, f"unexpected SQL: {sql}"
        return match.group(1), [c.strip() for c in match.group(2).split(',')]

    def values(self, table):
        """{key_tuple: value} for assertions about what the table now holds."""
        return {k: r['value'] for k, r in self.rows.get(table, {}).items()}

    def total(self, table):
        from decimal import Decimal
        return sum((r['value'] for r in self.rows.get(table, {}).values()),
                   Decimal('0'))

    def row_count(self, table):
        return len(self.rows.get(table, {}))


@pytest.fixture
def fake_questdb(monkeypatch):
    """A QuestDBWriter whose psycopg connection is a FakeQuestDB.

    Patches the writer's `_connect` rather than the psycopg module: the SQL and
    the executemany/commit/rollback flow are still exercised, only the socket is
    replaced.
    """
    import questdb_writer

    fake = FakeQuestDB()

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def executemany(self, sql, params):
            fake.execute(sql, list(params))

    class FakeConn:
        closed = False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            fake.committed += 1

        def rollback(self):
            fake.rolled_back += 1

    writer = questdb_writer.QuestDBWriter(dsn='postgresql://fake')
    monkeypatch.setattr(writer, '_connect', lambda: FakeConn())
    fake.writer = writer
    return fake
