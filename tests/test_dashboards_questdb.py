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


@pytest.fixture(params=[p[0] for p in PORTS])
def questdb_dashboard(request):
    return load(request.param)


# --------------------------------------------------------------------------
# Structure preserved from the VM original
# --------------------------------------------------------------------------

@pytest.mark.parametrize('port,original', PORTS)
def test_ported_panels_keep_the_original_structure(port, original):
    """Everything except datasource/targets/description must be identical.

    This is what makes the two dashboards comparable. `targets` differ by design
    (PromQL -> SQL) and `description` is added to explain the conversion; any
    other drift -- unit, gridPos, overrides, stacking, legend calcs -- means the
    panels no longer render the same way and a side-by-side check proves nothing.
    """
    ported = panels_by_id(load(port))
    source = panels_by_id(load(original))

    ignored = {'datasource', 'targets', 'description'}
    for panel_id, panel in ported.items():
        assert panel_id in source, f"panel {panel_id} does not exist in {original}"
        want = {k: v for k, v in source[panel_id].items() if k not in ignored}
        got = {k: v for k, v in panel.items() if k not in ignored}
        assert got == want, f"panel {panel_id} ({panel.get('title')}) drifted"


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
        assert '$__sampleByInterval' in sql, (title, ref)
        assert '$__timeFilter(ts)' in sql, (title, ref)


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


def test_queries_target_the_right_table(questdb_dashboard):
    """E66 and E31 live in separate tables; mixing them double-counts.

    cel_community_energy already contains what cel_energy sums to, which is the
    whole reason the schema splits them (see questdb_schema.sql).
    """
    is_e31 = 'e31' in questdb_dashboard['uid']
    expected = 'cel_community_energy' if is_e31 else 'cel_energy'
    for title, ref, sql in sql_targets(questdb_dashboard):
        tables = set(re.findall(r'FROM\s+(cel_\w+)', sql))
        assert tables == {expected}, (title, ref, tables)


def test_time_column_is_aliased_and_ordered(questdb_dashboard):
    """Grafana needs a `time` column; unordered rows render as a scribble."""
    for title, ref, sql in sql_targets(questdb_dashboard):
        assert re.search(r'\bts\s+AS\s+time\b', sql), (title, ref)
        assert 'ORDER BY time' in sql, (title, ref)


@pytest.mark.parametrize('port,original', PORTS)
def test_series_name_matches_the_original_legend(port, original):
    """SQL has no legendFormat: the column alias becomes the series name.

    Pinned against the ORIGINAL's legendFormat, per refId, not against the
    panel's `byName` overrides. Matching the legend is what keeps the two
    dashboards comparable -- same series names, same colour assignment, so a
    side-by-side difference can only come from the data.

    Deliberately not asserting that overrides match: in
    grafana-dashboard-e31-v2.json they do not, in *any* of its four timeseries
    panels (panel 7 colours 'CEL Local'/'Grid'/'Total' while its series are
    'From CEL'/'From Grid'). Those overrides are dead in the VM dashboard today,
    so reproducing the mismatch is what "same structure" means -- asserting they
    line up would fail on a pre-existing bug this migration is not fixing.
    """
    source = panels_by_id(load(original))
    for panel in load(port)['panels']:
        for target in panel.get('targets', []):
            aliases = re.findall(r'AS\s+"([^"]+)"', target['rawSql'])
            assert len(aliases) == 1, (panel.get('title'), target['refId'])
            want = next(t.get('legendFormat')
                        for t in source[panel['id']]['targets']
                        if t.get('refId') == target['refId'])
            assert aliases[0] == want, (
                f"panel {panel['id']} refId {target['refId']}: alias "
                f"{aliases[0]!r} != original legend {want!r}")


def test_no_project_label_filter_remains(questdb_dashboard):
    """`project="cel"` was a VM-namespace workaround; the table scopes it now.

    Scoped to the SQL only. The recorded `meta.promql` is the original expression
    verbatim and must keep its project filter -- scanning the whole JSON would
    fail on the provenance note it is supposed to preserve.
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
# Provenance
# --------------------------------------------------------------------------

def test_original_promql_is_recorded_next_to_each_query(questdb_dashboard):
    """Each target keeps the PromQL it replaced, for the Phase 6 comparison."""
    for panel in questdb_dashboard['panels']:
        for target in panel.get('targets', []):
            promql = target.get('meta', {}).get('promql')
            assert promql, (panel.get('title'), target.get('refId'))


@pytest.mark.parametrize('port,original', PORTS)
def test_recorded_promql_matches_the_original_dashboard(port, original):
    """The recorded PromQL must be the real expression, not a paraphrase.

    Otherwise the provenance note drifts from the query it claims to document and
    the Phase 6 comparison is checking the wrong pair.
    """
    source = panels_by_id(load(original))
    for panel in load(port)['panels']:
        for target in panel.get('targets', []):
            promql = target['meta']['promql']
            source_exprs = {t.get('expr')
                            for t in source[panel['id']].get('targets', [])}
            assert promql in source_exprs, (
                f"panel {panel['id']} refId {target['refId']}: recorded PromQL "
                f"is not among the original's expressions")
