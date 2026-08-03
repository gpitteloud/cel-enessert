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

# vm_upsert imports `requests`, which the container installs but a bare checkout
# may not have. Tests never make real HTTP calls (see FakeVictoriaMetrics), so a
# stub is enough to import the module - and it keeps the vm_upsert tests from
# silently skipping, which is where a wrong value in VM would slip through.
try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    import types as _types

    def _no_http(*args, **kwargs):
        raise AssertionError(
            'tests must not perform real HTTP requests - use the fake_vm fixture')

    _requests_stub = _types.ModuleType('requests')
    _requests_stub.post = _no_http
    _requests_stub.get = _no_http
    _requests_stub.exceptions = _types.SimpleNamespace(RequestException=Exception)
    sys.modules['requests'] = _requests_stub

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
# Fake VictoriaMetrics
# --------------------------------------------------------------------------

class FakeVictoriaMetrics:
    """In-memory stand-in that reproduces the two VM behaviours that matter.

    1. **No per-timestamp overwrite.** Importing a sample whose
       ``(metric_name, labels, timestamp)`` already exists keeps the **maximum**
       of the two values. This is the real behaviour and the reason
       ``vm_upsert`` exists: a downward revision cannot be applied by writing.
    2. ``/api/v1/admin/tsdb/delete_series`` drops whole series (VM has no
       per-timestamp delete), so a revised series must be deleted and replayed.

    Tests assert against ``data``: ``{selector: {timestamp_ms: value}}``.
    """

    def __init__(self):
        self.data = {}
        self.deletes = 0
        self.imports = 0
        self.fail_next_import = False
        self.fail_next_delete = False

    # Mimics requests.post(...) closely enough for vm_upsert.
    def post(self, url, data=None, headers=None, timeout=None):
        import json as _json
        import urllib.parse as _up

        class Response:
            status_code = 204
            text = ''

        resp = Response()

        if 'delete_series' in url:
            if self.fail_next_delete:
                self.fail_next_delete = False
                resp.status_code = 500
                resp.text = 'injected delete failure'
                return resp
            query = _up.parse_qs(_up.urlparse(url).query)
            selectors = query.get('match[]', [])
            assert selectors, f"delete_series called without match[]: {url}"
            for selector in selectors:
                self.data.pop(selector, None)
            self.deletes += 1
            return resp

        if self.fail_next_import:
            self.fail_next_import = False
            resp.status_code = 503
            resp.text = 'injected import failure'
            return resp

        self.imports += 1
        for line in (data or '').split('\n'):
            if not line:
                continue
            point = _json.loads(line)
            selector = _selector_for(point['metric'])
            series = self.data.setdefault(selector, {})
            for ts, value in zip(point['timestamps'], point['values']):
                current = series.get(ts)
                series[ts] = value if current is None else max(current, value)
        return resp

    def total(self):
        return sum(sum(series.values()) for series in self.data.values())

    def sample_count(self):
        return sum(len(series) for series in self.data.values())


def _selector_for(metric):
    # Imported lazily so conftest stays importable without scripts/ deps loaded.
    from vm_upsert import selector_for
    return selector_for(metric)


@pytest.fixture
def fake_vm(monkeypatch):
    """A FakeVictoriaMetrics wired into vm_upsert in place of `requests`."""
    import types as _types
    import vm_upsert

    fake = FakeVictoriaMetrics()
    monkeypatch.setattr(vm_upsert, 'requests', _types.SimpleNamespace(
        post=fake.post,
        exceptions=_types.SimpleNamespace(RequestException=Exception),
    ))
    return fake


@pytest.fixture
def sample_store(tmp_path):
    """A SampleStore backed by a throwaway SQLite file."""
    from vm_upsert import SampleStore
    store = SampleStore(tmp_path / 'vm_samples.db')
    yield store
    store.close()


# --------------------------------------------------------------------------
# Fake QuestDB
# --------------------------------------------------------------------------

class FakeQuestDB:
    """In-memory stand-in reproducing DEDUP UPSERT KEYS: last write wins.

    The contrast with FakeVictoriaMetrics is the entire point of the migration:
    VM keeps the MAXIMUM value for a duplicated key, QuestDB *replaces* the row.
    A downward revision therefore lands here and cannot land there.

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
