"""Tests for the QuestDB dashboard ports (MIGRATION_QUESTDB.md Phase 5).

The point of these dashboards is to be comparable against their VM originals
panel by panel, so the thing worth pinning is that *only* the datasource and the
queries changed. A stray fieldConfig or gridPos difference would make a visual
comparison meaningless -- two panels that look different because of styling, not
because of the data, is exactly the false signal Phase 6 must not get.

They also catch the SQL mistakes that would silently produce plausible-looking
but wrong numbers: `sum(value) * 4` without the inner 15m slot average, or a
leftover PromQL macro that the QuestDB plugin does not implement.
"""
import json
import re
from pathlib import Path

import pytest

DASHBOARDS = Path(__file__).resolve().parent.parent / 'grafana-dashboards'

# (questdb port, vm original)
PORTS = [
    ('cel_energy_overview_questdb.json', 'cel_energy_overview.json'),
    ('grafana-dashboard-e31-v2-questdb.json', 'grafana-dashboard-e31-v2.json'),
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


@pytest.fixture(params=[p[0] for p in PORTS])
def questdb_dashboard(request):
    return load(request.param)


# --------------------------------------------------------------------------
# Structure preserved from the VM original
# --------------------------------------------------------------------------

@pytest.mark.parametrize('port,original', PORTS)
def test_every_panel_and_expression_is_ported(port, original):
    """The port must be complete: same panel ids, same refIds per panel.

    Phase 5 was done incrementally (one panel first, to settle the query shape),
    so "the dashboard renders" was never evidence that all of it had been
    converted -- a missing panel just is not there, and a missing target is a
    legend entry nobody counts. This is the check that the migration finished.
    """
    ported, source = panels_by_id(load(port)), panels_by_id(load(original))
    assert set(ported) == set(source), (
        f"panel ids differ: missing {sorted(set(source) - set(ported))}, "
        f"extra {sorted(set(ported) - set(source))}")

    for panel_id, panel in ported.items():
        want = {t['refId'] for t in source[panel_id].get('targets', [])}
        got = {t['refId'] for t in panel.get('targets', [])}
        assert got == want, (
            f"panel {panel_id} ({panel.get('title')}): refIds {sorted(got)} "
            f"!= original {sorted(want)}")


@pytest.mark.parametrize('port,original', PORTS)
def test_panels_are_ordered_by_layout(port, original):
    """Grafana renders by gridPos, but the JSON order is what a human reads.

    Appending ported panels leaves the file in conversion order rather than
    layout order, so the next person editing it reads the panels in a different
    sequence from the one on screen.
    """
    positions = [(p['gridPos']['y'], p['gridPos']['x'])
                 for p in load(port)['panels']]
    assert positions == sorted(positions), 'panels are not in layout order'


@pytest.mark.parametrize('port,original', PORTS)
def test_ported_panels_keep_the_original_structure(port, original):
    """Everything except datasource/targets/description must be identical.

    This is what makes the two dashboards comparable. `targets` differ by design
    (PromQL -> SQL) and `description` is added to explain the conversion; any
    other drift -- unit, gridPos, stacking, legend calcs -- means the panels no
    longer render the same way and a side-by-side check proves nothing.

    `fieldConfig.overrides` is also exempt, and that exemption is narrow on
    purpose: `fieldConfig.defaults` (unit, colour mode, axis, stacking) is still
    compared key-for-key below, because that is the styling a visual comparison
    depends on. Overrides have to differ because the two datasources name their
    series differently -- see test_series_is_renamed_by_frame_ref_id.
    """
    ported = panels_by_id(load(port))
    source = panels_by_id(load(original))

    ignored = {'datasource', 'targets', 'description', 'fieldConfig'}
    for panel_id, panel in ported.items():
        assert panel_id in source, f"panel {panel_id} does not exist in {original}"
        want = {k: v for k, v in source[panel_id].items() if k not in ignored}
        got = {k: v for k, v in panel.items() if k not in ignored}
        assert got == want, f"panel {panel_id} ({panel.get('title')}) drifted"

        # fieldConfig minus overrides: everything that styles the panel itself.
        assert panel.get('fieldConfig', {}).get('defaults') \
            == source[panel_id].get('fieldConfig', {}).get('defaults'), \
            f"panel {panel_id} ({panel.get('title')}): fieldConfig.defaults drifted"
        assert set(panel.get('fieldConfig', {})) \
            == set(source[panel_id].get('fieldConfig', {})), \
            f"panel {panel_id}: unexpected fieldConfig keys"


@pytest.mark.parametrize('port,original', PORTS)
def test_dashboard_scaffolding_matches(port, original):
    """Time range, tooltip mode, schemaVersion etc. carry over unchanged."""
    ported, source = load(port), load(original)

    for key in ('schemaVersion', 'graphTooltip', 'time', 'style', 'editable',
                'fiscalYearStartMonth', 'liveNow', 'refresh', 'timezone',
                'weekStart', 'annotations'):
        assert ported[key] == source[key], key


@pytest.mark.parametrize('port,original', PORTS)
def test_uid_and_title_are_distinct_from_the_original(port, original):
    """Both dashboards are provisioned at once; a shared uid would overwrite."""
    ported, source = load(port), load(original)
    assert ported['uid'] != source['uid']
    assert ported['title'] != source['title']
    assert 'questdb' in ported['tags']


# --------------------------------------------------------------------------
# Datasource wiring
# --------------------------------------------------------------------------

def test_no_panel_still_points_at_victoriametrics(questdb_dashboard):
    """A missed datasource silently keeps reading VM and the port looks fine."""
    blob = json.dumps(questdb_dashboard)
    assert 'VictoriaMetrics' not in blob
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
            assert 'expr' not in target, 'leftover PromQL expression'


def test_no_promql_macros_survive(questdb_dashboard):
    """$__interval and $__range are Prometheus macros the plugin does not have.

    Left in place they do not error loudly -- they reach QuestDB as literal text
    and the query fails or, worse, the panel just shows nothing.
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


@pytest.mark.parametrize('port,original', PORTS)
def test_kw_scaling_matches_the_original_per_target(port, original):
    """The *4 must be present exactly where the PromQL had it.

    Caught a real slip: panel 11 (a kWh piechart) was generated from panel 10's
    kW template, so every slice was 4x too large. Both panels show production
    split cel/grid and differ only in unit, which is exactly the pair a
    copy-paste conflates -- and a pie chart of proportions looks correct while
    every absolute value is wrong.

    Pinned per target against the original expression rather than against the
    panel's unit: the unit says what the number means, the original says what it
    was, and only the latter can catch a panel whose unit is also wrong.
    """
    source = panels_by_id(load(original))
    for panel in load(port)['panels']:
        for target in panel.get('targets', []):
            expr = next(t.get('expr', '') or ''
                        for t in source[panel['id']]['targets']
                        if t['refId'] == target['refId'])
            want = '*4' in expr.replace(' ', '')
            got = '* 4' in target['rawSql']
            assert got == want, (
                f"panel {panel['id']} ({panel.get('title')}) refId "
                f"{target['refId']}: kW scaling {'added' if got else 'dropped'}; "
                f"original {'has' if want else 'has no'} *4")


@pytest.mark.parametrize('port,original', PORTS)
def test_kw_scaling_agrees_with_the_panel_unit(port, original):
    """A *4 turns kWh into kW, so the unit must say so.

    Independent of the test above: that one pins the port against the original,
    this one pins the original's own internal consistency. A panel labelled
    kwatth whose query scales by 4 is wrong no matter which side introduced it.
    """
    for panel in load(port)['panels']:
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


@pytest.mark.parametrize('port,original', PORTS)
def test_cross_table_queries_are_only_the_validation_panels(port, original):
    """Guards the allow-list against drift in both directions.

    A stale entry would silently license a double-counting query on a panel that
    no longer needs it; a missing one would have been caught by the test above.
    Also pins that these panels really are the validation ones, by title.
    """
    ported = panels_by_id(load(port))
    for panel_id, ref in CROSS_TABLE_TARGETS:
        if panel_id not in ported:
            continue
        assert 'Validation' in ported[panel_id]['title'], (
            f"panel {panel_id} ({ported[panel_id]['title']!r}) is allow-listed "
            f"for cross-table reads but is not a validation panel")
        sql = next(t['rawSql'] for t in ported[panel_id]['targets']
                   if t['refId'] == ref)
        assert 'cel_energy' in sql, (panel_id, ref)


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


@pytest.mark.parametrize('port,original', PORTS)
def test_series_name_matches_the_original_legend(port, original):
    """SQL has no legendFormat: the column alias becomes the series name.

    Pinned against the ORIGINAL's legendFormat, per refId, not against the
    panel's `byName` overrides. Matching the legend is what keeps the two
    dashboards comparable -- same series names, same colour assignment, so a
    side-by-side difference can only come from the data.

    Deliberately not asserting that the ORIGINAL's overrides match: in
    grafana-dashboard-e31-v2.json they did not, in *any* of its four timeseries
    panels (panel 7 coloured 'CEL Local'/'Grid'/'Total' while its series were
    'From CEL'/'From Grid'). Those were fixed in the VM dashboards separately;
    what this test pins is the alias, which is the series name Grafana starts
    from before displayName renames it.
    """
    source = panels_by_id(load(original))
    for panel in load(port)['panels']:
        for target in panel.get('targets', []):
            aliases = re.findall(r'AS\s+"([^"]+)"', target['rawSql'])
            assert len(aliases) == 1, (panel.get('title'), target['refId'])
            want = next(t.get('legendFormat')
                        for t in source[panel['id']]['targets']
                        if t.get('refId') == target['refId'])
            if want == '__auto':
                # PromQL's '__auto' means "name it from the metric and labels",
                # which has no SQL equivalent -- and on a single-series stat or
                # gauge it rendered as the whole label set. The panel title is
                # what the reader actually sees, so the alias uses that; assert
                # it is deliberate rather than accidentally the refId.
                assert aliases[0] == panel['title'], (
                    f"panel {panel['id']} refId {target['refId']}: alias "
                    f"{aliases[0]!r} should be the panel title for a "
                    f"'__auto' legend")
                continue
            assert aliases[0] == want, (
                f"panel {panel['id']} refId {target['refId']}: alias "
                f"{aliases[0]!r} != original legend {want!r}")


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


def test_no_byname_override_survives_the_port(questdb_dashboard):
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


@pytest.mark.parametrize('port,original', PORTS)
def test_colours_survive_the_override_rewrite(port, original):
    """Swapping byName for byFrameRefID must not drop the colour assignment.

    The rename and the colour ride on the same override, so it is easy to port
    the displayName and silently lose the fixedColor -- which looks like a
    working panel with the wrong colours.
    """
    source = panels_by_id(load(original))
    for panel in load(port)['panels']:
        if panel['type'] == 'row':
            continue
        want = {}
        for o in source[panel['id']]['fieldConfig']['overrides']:
            if o['matcher']['id'] == 'byName':
                for prop in o['properties']:
                    if prop['id'] == 'color':
                        want[o['matcher']['options']] = prop['value']

        got = {}
        for o in panel['fieldConfig']['overrides']:
            props = {p['id']: p['value'] for p in o['properties']}
            if 'color' in props:
                got[props['displayName']] = props['color']

        assert got == want, (
            f"panel {panel['id']} ({panel.get('title')}): colour assignment "
            f"changed; {got} != {want}")


def test_no_project_label_filter_remains(questdb_dashboard):
    """`project="cel"` was a VM-namespace workaround; the table scopes it now.

    Scoped to the SQL only. The recorded `promqlOriginal` is the original
    expression verbatim and must keep its project filter -- scanning the whole
    JSON would fail on the provenance note it is supposed to preserve.
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
            assert 'editorMode' not in target, (
                f"{where}: editorMode is not a QuestDBSQLQuery field")


def test_meta_carries_only_fields_the_plugin_declares(questdb_dashboard):
    """An unknown key in `meta` is round-tripped through the query builder."""
    allowed = {'timezone', 'builderOptions'}
    for panel in questdb_dashboard['panels']:
        if panel['type'] == 'row':
            continue
        for target in panel['targets']:
            extra = set(target.get('meta', {})) - allowed
            assert not extra, (panel.get('title'), target.get('refId'), extra)


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

def test_original_promql_is_recorded_next_to_each_query(questdb_dashboard):
    """Each target keeps the PromQL it replaced, for the Phase 6 comparison.

    Stored as a top-level `promqlOriginal`, NOT under `meta`. The plugin's
    QuestDBSQLQuery declares `meta` as `{timezone?, builderOptions?}`, and it
    round-trips whatever it finds there through its query builder -- an unknown
    key inside `meta` is not a safe place to park a note. Unknown top-level keys
    are ignored, so they survive an edit-and-save in the UI.
    """
    for panel in questdb_dashboard['panels']:
        for target in panel.get('targets', []):
            if panel['type'] == 'row':
                continue
            assert target.get('promqlOriginal'), (panel.get('title'),
                                                  target.get('refId'))
            assert 'promql' not in target.get('meta', {}), (
                'meta is a typed plugin field; keep provenance top-level')


@pytest.mark.parametrize('port,original', PORTS)
def test_recorded_promql_matches_the_original_dashboard(port, original):
    """The recorded PromQL must be the real expression, not a paraphrase.

    Otherwise the provenance note drifts from the query it claims to document and
    the Phase 6 comparison is checking the wrong pair.
    """
    source = panels_by_id(load(original))
    for panel in load(port)['panels']:
        for target in panel.get('targets', []):
            promql = target['promqlOriginal']
            source_exprs = {t.get('expr')
                            for t in source[panel['id']].get('targets', [])}
            assert promql in source_exprs, (
                f"panel {panel['id']} refId {target['refId']}: recorded PromQL "
                f"is not among the original's expressions")
