"""Tests for the Grafana dashboards' QuestDB queries.

These catch the mistakes that produce a plausible-looking but wrong number, which
Grafana will render without complaint:

  * `sum(value) * 4` without an inner 15-min slot average -- correct only when the
    bucket happens to be 15 min, 4x wrong at 1h.
  * a DECIMAL column returned un-cast, which the plugin has no converter for, so
    the panel reports "Data is missing a number field".
  * a bare `0.5` compared against DECIMAL(12,3), which QuestDB does not implicitly
    convert.
  * an unscoped `cel_energy` read, which sums 8 meters that carry no community at
    all and are absent from the E31 aggregate they are being compared against.

Plugin-contract expectations are pinned against questdb-questdb-datasource v0.1.8
and explained where they are asserted; several of them cost real debugging time
because the failure mode is an empty panel rather than an error. See QUESTDB.md.

test_dashboards.py covers what is datasource-independent (override matchers, uid
uniqueness); this file covers the SQL and the plugin wiring.
"""
import json
import re
from pathlib import Path

import pytest

DASHBOARDS = Path(__file__).resolve().parent.parent / 'grafana-dashboards'

DASHBOARD_FILES = [
    'cel_energy_overview.json',
    'grafana-dashboard-e31-v2.json',
]

QUESTDB_TYPE = 'questdb-questdb-datasource'


def load(name):
    return json.loads((DASHBOARDS / name).read_text())


def panels_by_id(dashboard):
    return {p['id']: p for p in dashboard['panels']}


def sql_targets(dashboard):
    """Every (panel_title, refId, rawSql) in a dashboard."""
    for panel in dashboard['panels']:
        for target in panel.get('targets', []):
            if 'rawSql' in target:
                yield panel.get('title', ''), target.get('refId'), target['rawSql']


# Grafana's Format enum, as the plugin declares it (src/types.ts).
FORMAT_TIMESERIES, FORMAT_TABLE = 0, 1

# Panels whose query returns ONE scalar row for the whole time range rather than
# a series: a gauge reduces to a single percentage, so bucketing it would be
# meaningless. These use format=TABLE and therefore have no `ts AS time` column
# and no SAMPLE BY -- the checks that assume a series are keyed off this.
SCALAR_PANEL_TYPES = {'gauge'}


def scalar_targets(dashboard):
    """(title, refId, rawSql) for the single-row/whole-range queries."""
    for panel in dashboard['panels']:
        if panel['type'] not in SCALAR_PANEL_TYPES:
            continue
        for target in panel.get('targets', []):
            yield panel.get('title', ''), target.get('refId'), target['rawSql']


def series_targets(dashboard):
    """(title, refId, rawSql) for the time-bucketed queries."""
    scalar = {(t, r) for t, r, _ in scalar_targets(dashboard)}
    for title, ref, sql in sql_targets(dashboard):
        if (title, ref) not in scalar:
            yield title, ref, sql


@pytest.fixture(params=DASHBOARD_FILES)
def questdb_dashboard(request):
    return load(request.param)


def test_every_dashboard_file_is_covered():
    """A dashboard added to the folder but not to DASHBOARD_FILES is untested."""
    on_disk = {p.name for p in DASHBOARDS.glob('*.json')}
    assert on_disk == set(DASHBOARD_FILES), (
        f"untested: {sorted(on_disk - set(DASHBOARD_FILES))}, "
        f"missing from disk: {sorted(set(DASHBOARD_FILES) - on_disk)}")


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

@pytest.mark.parametrize('dashboard_name', DASHBOARD_FILES)
def test_panels_are_ordered_by_layout(dashboard_name):
    """Grafana renders by gridPos, but the JSON order is what a human reads.

    A panel appended to the end of the file rather than inserted at its position
    leaves the JSON in a different sequence from the one on screen, so the next
    person editing it reads the panels out of order.
    """
    positions = [(p['gridPos']['y'], p['gridPos']['x'])
                 for p in load(dashboard_name)['panels']]
    assert positions == sorted(positions), 'panels are not in layout order'

# --------------------------------------------------------------------------
# Datasource wiring
# --------------------------------------------------------------------------
def test_no_panel_points_at_a_prometheus_datasource(questdb_dashboard):
    """These read SQL from QuestDB; a prometheus-typed datasource cannot work.

    Kept as an explicit assertion because the symptom is not obvious: the panel
    renders empty rather than reporting a type error.
    """
    blob = json.dumps(questdb_dashboard)
    assert '"prometheus"' not in blob

def test_every_datasource_reference_uses_the_provisioned_uid(questdb_dashboard):
    """uid must match grafana-provisioning/datasources/questdb.yaml."""
    for panel in questdb_dashboard['panels']:
        # The built-in annotation datasource is exempt.
        assert panel['datasource'] == {'type': QUESTDB_TYPE, 'uid': 'QuestDB'}, \
            panel.get('title')
        for target in panel.get('targets', []):
            assert target['datasource'] == {'type': QUESTDB_TYPE,
                                            'uid': 'QuestDB'}

def test_datasource_uid_matches_provisioning_file():
    """Pins the JSON against the YAML rather than trusting they agree."""
    yaml_text = (DASHBOARDS.parent / 'grafana-provisioning' / 'datasources'
                 / 'questdb.yaml').read_text()
    assert re.search(r'^\s*uid:\s*QuestDB\s*$', yaml_text, re.M)
    assert re.search(r'^\s*type:\s*' + re.escape(QUESTDB_TYPE) + r'\s*$',
                     yaml_text, re.M)


# --------------------------------------------------------------------------
# SQL correctness
# --------------------------------------------------------------------------
def test_every_target_has_sql(questdb_dashboard):
    for panel in questdb_dashboard['panels']:
        if panel['type'] == 'row':
            continue
        assert panel.get('targets'), panel.get('title')
        for target in panel['targets']:
            assert target.get('rawSql'), (panel.get('title'), target.get('refId'))

def test_no_prometheus_macros_survive(questdb_dashboard):
    """$__interval and $__range are Prometheus macros the plugin does not have.

    The QuestDB plugin implements $__timeFilter and $__sampleByInterval. An
    unimplemented macro is not rejected -- it reaches QuestDB as literal text, so
    the query fails or the panel simply shows nothing.
    """
    for title, ref, sql in sql_targets(questdb_dashboard):
        assert '$__interval' not in sql, (title, ref)
        assert '$__range' not in sql, (title, ref)
        # Every query must be range-bounded, series or scalar. A gauge without
        # it would silently aggregate all of history and ignore the time picker.
        assert '$__timeFilter(ts)' in sql, (title, ref)

    for title, ref, sql in series_targets(questdb_dashboard):
        assert '$__sampleByInterval' in sql, (title, ref)

    for title, ref, sql in scalar_targets(questdb_dashboard):
        # A scalar reduces the whole range to one row; bucketing it would return
        # a series the gauge would then reduce again with lastNotNull, i.e. the
        # last bucket instead of the range total.
        assert 'SAMPLE BY' not in sql, (title, ref)

@pytest.mark.parametrize('dashboard_name', DASHBOARD_FILES)
def test_kw_scaling_agrees_with_the_panel_unit(dashboard_name):
    """A *4 turns a 15-min kWh slot into kW, so the unit must say so.

    Caught a real slip: panel 11 (a kWh piechart) was built from panel 10's kW
    template, so every slice was 4x too large. Both panels show production split
    cel/grid and differ only in unit, which is exactly the pair a copy-paste
    conflates -- and a pie chart of proportions looks right while every absolute
    value is wrong.
    """
    for panel in load(dashboard_name)['panels']:
        unit = panel.get('fieldConfig', {}).get('defaults', {}).get('unit')
        if unit not in ('kwatt', 'kwatth'):
            continue
        for target in panel.get('targets', []):
            scaled = '* 4' in target['rawSql']
            assert scaled == (unit == 'kwatt'), (
                f"panel {panel['id']} ({panel.get('title')}) refId "
                f"{target['refId']}: unit {unit!r} but query "
                f"{'scales' if scaled else 'does not scale'} by 4")

def test_kw_conversion_averages_before_scaling(questdb_dashboard):
    """The *4 must apply to a 15-min slot average, not to a bucket sum.

    `sum(value) * 4` is correct only when the bucket IS 15 min. At 1h it reports
    16 kW for four 1 kWh slots instead of 4. The guard is structural: any query
    scaling by 4 must have an inner `SAMPLE BY 15m` and average over it.
    """
    for title, ref, sql in sql_targets(questdb_dashboard):
        if '* 4' not in sql:
            continue
        assert 'SAMPLE BY 15m' in sql, (title, ref)
        assert re.search(r'avg\(\w+\)\s*\*\s*4', sql), (title, ref)
        assert not re.search(r'sum\(value\)\s*\*\s*4', sql), (
            f"{title}/{ref}: sums the bucket then scales -- wrong above 15m")

def test_value_columns_are_cast_to_double(questdb_dashboard):
    """The Grafana plugin has no DECIMAL converter, so a raw DECIMAL is a string.

    pkg/converters/converters.go maps exactly six type names -- BOOL, INT2,
    FLOAT4, FLOAT8, TIMESTAMP, TIMESTAMP_NS -- by EXACT string equality, with no
    pattern fallback:

        func GetConverter(columnType string) sqlutil.Converter {
            converter, ok := Converters[columnType]
            ...
            return sqlutil.Converter{}      // <- DECIMAL lands here
        }

    An unmatched type returns a zero-value Converter, the column arrives as a
    string field, and the panel reports "Data is missing a number field". The
    time column converts fine, which is why the panel renders at all.

    The cast is on the OUTER expression only. The inner `sum(value)` must stay
    DECIMAL: that is where the exactness matters (summing the provider's 3-decimal
    values), and DECIMAL(12,3) is only widened to float once, at the wire, where
    Grafana would convert it to float64 regardless.
    """
    for title, ref, sql in sql_targets(questdb_dashboard):
        outer = sql.split('FROM (')[0]
        for alias_expr in re.findall(r'([^,]+?)\s+AS\s+"[^"]+"', outer):
            assert re.search(r'cast\(.+ AS DOUBLE\)', alias_expr, re.I), (
                f"{title}/{ref}: {alias_expr.strip()!r} returns DECIMAL "
                f"un-cast; the plugin has no DECIMAL converter")

def test_the_inner_slot_sum_stays_decimal(questdb_dashboard):
    """Casting inside the subquery would make the slot sums float arithmetic.

    That reintroduces exactly the binary-rounding error DECIMAL(12,3) exists to
    avoid, and questdb_writer rejects a float `value` for the same reason.
    """
    for title, ref, sql in sql_targets(questdb_dashboard):
        if 'FROM (' not in sql:
            continue
        inner = sql.split('FROM (', 1)[1]
        assert not re.search(r'cast\(\s*sum\(value\)', inner, re.I), (
            f"{title}/{ref}: cast the outer average, not the inner slot sum")

# The validation panels exist precisely to compare the two tables against each
# other, so they are the one place a cross-table query is correct. Listed by
# (panel id, refId) rather than allowed dashboard-wide: anywhere else, reading
# both tables double-counts, since cel_community_energy already contains what
# cel_energy sums to (see questdb_schema.sql).
CROSS_TABLE_TARGETS = {
    (13, 'B'),   # Sum(E66) beside the E31 consumption total
    (14, 'B'),   # ... and production
    (15, 'A'),   # E31 - Sum(E66), consumption
    (15, 'B'),   # ... and production
}


def test_queries_target_the_right_table(questdb_dashboard):
    """E66 and E31 live in separate tables; mixing them double-counts.

    cel_community_energy already contains what cel_energy sums to, which is the
    whole reason the schema splits them (see questdb_schema.sql). The validation
    panels are the deliberate exception -- comparing the two IS their purpose.
    """
    is_e31 = 'e31' in questdb_dashboard['uid']
    expected = 'cel_community_energy' if is_e31 else 'cel_energy'
    for panel in questdb_dashboard['panels']:
        for target in panel.get('targets', []):
            tables = set(re.findall(r'FROM\s+(cel_\w+)', target['rawSql']))
            where = (panel.get('title'), target['refId'], tables)
            if (panel['id'], target['refId']) in CROSS_TABLE_TARGETS:
                assert tables, where
                assert tables <= {'cel_energy', 'cel_community_energy'}, where
            else:
                assert tables == {expected}, where

@pytest.mark.parametrize('dashboard_name', DASHBOARD_FILES)
def test_cross_table_queries_are_only_the_validation_panels(dashboard_name):
    """Guards the allow-list against drift in both directions.

    A stale entry would silently license a double-counting query on a panel that
    no longer needs it; a missing one would have been caught by the test above.
    Also pins that these panels really are the validation ones, by title.
    """
    panels = panels_by_id(load(dashboard_name))
    for panel_id, ref in CROSS_TABLE_TARGETS:
        if panel_id not in panels:
            continue
        assert 'Validation' in panels[panel_id]['title'], (
            f"panel {panel_id} ({panels[panel_id]['title']!r}) is allow-listed "
            f"for cross-table reads but is not a validation panel")
        sql = next(t['rawSql'] for t in panels[panel_id]['targets']
                   if t['refId'] == ref)
        assert 'cel_energy' in sql, (panel_id, ref)

# The community whose aggregate the E31 dashboard is about. Every cel_energy
# read on that dashboard must be scoped to it, so the per-meter sum covers the
# same population as the aggregate it is compared against.
E31_COMMUNITY = '101110-002726'


@pytest.mark.parametrize('dashboard_name', DASHBOARD_FILES)
def test_every_cel_energy_read_is_scoped_to_the_community(dashboard_name):
    """A cel_energy query with no community_id sums meters outside the community.

    The provider delivers E66 files for 8 meters that carry no <Community>
    element at all, so their community_id is NULL and they are not in the E31
    aggregate -- yet an unscoped sum() picks them up. Measured on 70 days of real
    deliveries that inflated Sum(E66) by +24% for consumption and +27% for
    production, which was the entire apparent validation gap: with the filter the
    two sides agree to ~1.5%. See QUESTDB.md.

    Nothing errors; the panel just plots a number for a different population
    than the series beside it, which is the worst kind of wrong for a panel
    whose whole job is to say "these two should match".
    """
    if 'e31' not in dashboard_name:
        return
    panels = panels_by_id(load(dashboard_name))
    unscoped = []
    for panel_id, panel in sorted(panels.items()):
        for target in panel.get('targets', []):
            sql = target.get('rawSql', '')
            # Checked per SELECT block, not per statement: panel 15 UNIONs a
            # cel_community_energy read with a cel_energy one, and that first
            # block's filter must not be allowed to stand in for the second's.
            for block in re.split(r'\bUNION\s+ALL\b|\bFROM\s*\(', sql):
                if not re.search(r'\bFROM\s+cel_energy\b', block):
                    continue
                if not re.search(
                        rf"community_id\s*=\s*'{re.escape(E31_COMMUNITY)}'",
                        block):
                    unscoped.append(
                        f"{dashboard_name} panel {panel_id} ({panel['title']!r}) target "
                        f"{target['refId']}: a cel_energy read with no "
                        f"community_id filter")
    assert not unscoped, (
        'unscoped cel_energy reads (they include meters that are not in the '
        'E31 aggregate):\n' + '\n'.join(unscoped))

def test_time_column_is_aliased_and_ordered(questdb_dashboard):
    """Grafana needs a `time` column; unordered rows render as a scribble."""
    for title, ref, sql in series_targets(questdb_dashboard):
        assert re.search(r'\bts\s+AS\s+time\b', sql), (title, ref)
        assert 'ORDER BY time' in sql, (title, ref)

def test_scalar_queries_return_one_row(questdb_dashboard):
    """A gauge query must reduce, not bucket -- and so must not select `ts`.

    Selecting the timestamp alongside a bare aggregate would either fail or, if
    QuestDB tolerated it, hand the gauge a time column it then reduces with
    lastNotNull -- reporting the newest bucket rather than the range.
    """
    for title, ref, sql in scalar_targets(questdb_dashboard):
        assert 'AS time' not in sql, (title, ref)
        assert 'ORDER BY' not in sql, (title, ref)
        assert re.match(r'\s*SELECT\s', sql), (title, ref)

def test_no_project_label_filter_remains(questdb_dashboard):
    """There is no `project` column; the table itself is the namespace.

    A leftover `project` predicate would be a query error, not a filter.
    """
    for title, ref, sql in sql_targets(questdb_dashboard):
        assert 'project' not in sql, (title, ref)

def test_decimal_literals_use_the_m_suffix(questdb_dashboard):
    """QuestDB does not implicitly convert double -> decimal.

    `value > 0.5` and `value > 0.5m` are different comparisons against a
    DECIMAL(12,3) column, so a bare float literal is a silent wrong answer.
    Integer literals (like the *4 scaling) are fine and must stay integers --
    4.000m would promote DECIMAL(12,3) to DECIMAL128 and lose the fast path.
    """
    for title, ref, sql in sql_targets(questdb_dashboard):
        for literal in re.findall(r'(?<![\w.])(\d+\.\d+)(?!m)', sql):
            pytest.fail(f"{title}/{ref}: bare decimal literal {literal} "
                        f"(needs an 'm' suffix against a DECIMAL column)")


# --------------------------------------------------------------------------
# Legend and series naming
# --------------------------------------------------------------------------
def test_series_is_renamed_by_frame_ref_id(questdb_dashboard):
    """Each target needs a byFrameRefID displayName override, or the legend
    reads "A From CEL" instead of "From CEL".

    The plugin returns one frame per target and sets frame.Name to the refId.
    When a panel has more than one frame, Grafana prefixes the frame name to the
    field name -- so a two-target panel legends as "A From CEL" / "B From Grid",
    while a single-target panel would look fine. That is why this only shows up
    on the multi-series panels.

    Matched byFrameRefID, not byName: the name Grafana is matching against at
    override time is the prefixed one, so a `byName: "From CEL"` override matches
    nothing and is silently ignored -- taking the panel's colours with it (see
    test_dashboards.py for that failure mode). refId is stable and is what the
    prefix is derived from.
    """
    for panel in questdb_dashboard['panels']:
        if panel['type'] == 'row':
            continue
        overrides = panel['fieldConfig']['overrides']
        renamed = {}
        for o in overrides:
            if o['matcher']['id'] != 'byFrameRefID':
                continue
            for prop in o['properties']:
                if prop['id'] == 'displayName':
                    renamed[o['matcher']['options']] = prop['value']

        for target in panel['targets']:
            ref = target['refId']
            alias = re.findall(r'AS\s+"([^"]+)"', target['rawSql'])[0]
            assert ref in renamed, (
                f"panel {panel['id']} refId {ref}: no displayName override, so "
                f"the legend will read {ref!r} + {alias!r}")
            assert renamed[ref] == alias, (
                f"panel {panel['id']} refId {ref}: renamed to "
                f"{renamed[ref]!r} but the SQL alias is {alias!r}")

def test_no_byname_override_is_used(questdb_dashboard):
    """byName cannot work here: the name it matches carries the refId prefix.

    Left in place it is not an error, just dead config -- the panel silently
    falls back to palette-classic and the colours look like a choice.
    """
    for panel in questdb_dashboard['panels']:
        for o in panel.get('fieldConfig', {}).get('overrides', []):
            assert o['matcher']['id'] != 'byName', (
                f"panel {panel['id']}: byName override "
                f"{o['matcher']['options']!r} is dead once frames are "
                f"refId-prefixed; match byFrameRefID instead")


# --------------------------------------------------------------------------
# Plugin query contract
# --------------------------------------------------------------------------
#
# Pinned against questdb-questdb-datasource v0.1.8 src/types.ts:
#
#   export enum Format { TIMESERIES = 0, TABLE = 1, AUTO = 2 }
#   export interface QuestDBSQLQuery extends QuestDBQueryBase {
#     queryType: QueryType.SQL;
#     rawSql: string;
#     meta?: { timezone?: string; builderOptions?: SqlBuilderOptions };
#     format: Format;
#     selectedFormat: Format;
#     expand?: boolean;
#   }
#
# These fields are why both dashboards first rendered "No data": `format` was
# written as the string "time_series" (the Prometheus/Postgres spelling), which
# maps to no Format member. The plugin then cannot pick a response transform and
# returns an empty frame WITHOUT surfacing an error -- the panel looks like a
# query that legitimately matched nothing. Nothing else in this file would catch
# it, because the SQL is correct.

def test_targets_declare_the_plugin_query_shape(questdb_dashboard):
    """format/selectedFormat must be the numeric Format enum, not a string.

    TIMESERIES for bucketed queries, TABLE for the gauges' single scalar row.
    Both must agree: the plugin reads `format` but the query editor renders from
    `selectedFormat`, so a mismatch means editing the panel in the UI silently
    rewrites the query shape.
    """
    scalar = {(t, r) for t, r, _ in scalar_targets(questdb_dashboard)}
    for panel in questdb_dashboard['panels']:
        if panel['type'] == 'row':
            continue
        for target in panel['targets']:
            where = (panel.get('title'), target.get('refId'))
            assert target.get('queryType') == 'sql', where
            want = (FORMAT_TABLE if (panel.get('title'), target['refId']) in scalar
                    else FORMAT_TIMESERIES)
            # `is True/False` would also admit bools: True == 1 is True and
            # isinstance(True, int) is True, so check the type explicitly.
            for key in ('format', 'selectedFormat'):
                value = target.get(key)
                assert value == want and isinstance(value, int) \
                    and not isinstance(value, bool), \
                    f"{where}: {key}={value!r}, want {want}"

def test_meta_carries_only_fields_the_plugin_declares(questdb_dashboard):
    """An unknown key in `meta` is round-tripped through the query builder."""
    allowed = {'timezone', 'builderOptions'}
    for panel in questdb_dashboard['panels']:
        if panel['type'] == 'row':
            continue
        for target in panel['targets']:
            extra = set(target.get('meta', {})) - allowed
            assert not extra, (panel.get('title'), target.get('refId'), extra)


def test_targets_carry_no_undeclared_fields(questdb_dashboard):
    """A key the plugin does not declare is dead weight the JSON cannot police.

    Two kinds accumulate. Fields from *another* datasource's query model
    (`editorMode`, `expr`, `legendFormat`) read as if they were doing something
    while the plugin ignores them entirely. Scratch notes recording where a query
    came from are worse: nothing consumes them, nothing validates them, and they
    go stale invisibly when the SQL beside them is edited.
    """
    allowed = {'datasource', 'format', 'selectedFormat', 'queryType', 'rawSql',
               'refId', 'hide', 'meta', 'expand'}
    for panel in questdb_dashboard['panels']:
        if panel['type'] == 'row':
            continue
        for target in panel['targets']:
            extra = set(target) - allowed
            assert not extra, (panel.get('title'), target.get('refId'), extra)


def test_every_target_has_exactly_one_aliased_column(questdb_dashboard):
    """The column alias IS the series name -- SQL has no legendFormat.

    Two aliased columns in one target would return two fields in one frame and
    the displayName override below could only rename one of them; none would
    leave the series called after its refId.
    """
    for title, ref, sql in sql_targets(questdb_dashboard):
        aliases = [a for a in re.findall(r'AS\s+"([^"]+)"', sql) if a != 'time']
        assert len(aliases) == 1, (title, ref, aliases)


def test_gauge_series_are_named_after_their_panel(questdb_dashboard):
    """A gauge shows one number, so its series name must read as a label.

    The panel title is what the reader actually sees, so the alias uses it --
    asserted so it stays deliberate rather than drifting to the refId or to a
    column expression.
    """
    for panel in questdb_dashboard['panels']:
        if panel['type'] not in SCALAR_PANEL_TYPES:
            continue
        for target in panel['targets']:
            alias = re.findall(r'AS\s+"([^"]+)"', target['rawSql'])[0]
            assert alias == panel['title'], (
                f"panel {panel['id']} refId {target['refId']}: alias {alias!r} "
                f"should be the panel title {panel['title']!r}")


# Panels that assign explicit series colours, and the colour each refId must get.
# Pinned by value because the colour and the displayName ride on the SAME
# override object: editing the rename is how the fixedColor gets dropped, and a
# panel that silently falls back to palette-classic looks like a choice rather
# than a bug.
EXPECTED_COLOURS = {
    'cel_energy_overview.json': {
        1: {'A': 'green', 'B': 'orange'},
        2: {'A': 'blue', 'B': 'yellow'},
    },
    'grafana-dashboard-e31-v2.json': {
        7: {'B': 'green', 'C': 'orange'},
        10: {'B': 'green', 'C': 'purple'},
        13: {'A': 'blue', 'B': 'red'},
        14: {'A': 'yellow', 'B': 'orange'},
    },
}


@pytest.mark.parametrize('dashboard_name', DASHBOARD_FILES)
def test_series_colours_are_assigned_by_frame_ref_id(dashboard_name):
    """Colours must survive edits to the overrides they share with displayName."""
    want = EXPECTED_COLOURS[dashboard_name]
    for panel_id, panel in sorted(panels_by_id(load(dashboard_name)).items()):
        got = {}
        for o in panel.get('fieldConfig', {}).get('overrides', []):
            props = {p['id']: p['value'] for p in o['properties']}
            if 'color' in props:
                assert o['matcher']['id'] == 'byFrameRefID', (
                    f"panel {panel_id}: colour matched by "
                    f"{o['matcher']['id']!r}, which does not match a "
                    f"refId-prefixed frame name")
                got[o['matcher']['options']] = props['color'].get('fixedColor')
        assert got == want.get(panel_id, {}), (
            f"panel {panel_id} ({panel.get('title')}): colours {got} != {want.get(panel_id, {})}")
