"""Tests for the Grafana dashboards themselves, VM and QuestDB alike.

Separate from test_dashboards_questdb.py, which is about the *migration* (do the
ports match their originals). This file is about defects a dashboard can carry
without anyone noticing, because Grafana does not complain:

A `byName` field override whose name matches no series is silently ignored. Every
timeseries panel in grafana-dashboard-e31-v2.json had this -- panel 7 coloured
'CEL Local'/'Grid'/'Total' while its series were 'From CEL'/'From Grid', so the
panel had been rendering on palette-classic defaults, not its intended colours,
since it was written. Nothing logs a warning; the only symptom is the wrong
colour, which looks like a choice rather than a bug.
"""
import json
import re
from pathlib import Path

import pytest

DASHBOARDS = Path(__file__).resolve().parent.parent / 'grafana-dashboards'

ALL_DASHBOARDS = sorted(p.name for p in DASHBOARDS.glob('*.json'))


def load(name):
    return json.loads((DASHBOARDS / name).read_text())


def series_names(panel):
    """Names Grafana will label this panel's series with.

    PromQL targets carry `legendFormat`; SQL targets have none, so the column
    alias becomes the series name. A dashboard may hold either kind.
    """
    names = set()
    for target in panel.get('targets', []):
        legend = target.get('legendFormat')
        if legend:
            names.add(legend)
        names.update(re.findall(r'AS\s+"([^"]+)"', target.get('rawSql', '')))
    return names


def by_name_overrides(panel):
    return [o for o in panel.get('fieldConfig', {}).get('overrides', [])
            if o.get('matcher', {}).get('id') == 'byName']


@pytest.fixture(params=ALL_DASHBOARDS)
def dashboard(request):
    return request.param, load(request.param)


def test_at_least_one_dashboard_is_checked():
    """Guards the glob: an empty parametrize list would pass everything."""
    assert len(ALL_DASHBOARDS) >= 4, ALL_DASHBOARDS


def test_every_byname_override_matches_a_real_series(dashboard):
    """A `byName` override naming no series is dead config, silently ignored.

    `__auto` and `legendFormat` templates are exempt: the series name is only
    known at query time, so a literal override name cannot be checked against
    them and may legitimately match.
    """
    name, data = dashboard
    dead = []
    for panel in data['panels']:
        if panel.get('type') == 'row':
            continue
        names = series_names(panel)
        # Templated legends resolve at query time; skip those panels entirely.
        if any('{{' in n or n == '__auto' for n in names):
            continue
        for override in by_name_overrides(panel):
            if override['matcher']['options'] not in names:
                dead.append(
                    f"{name} panel {panel['id']} ({panel.get('title')}): "
                    f"override {override['matcher']['options']!r} matches none "
                    f"of {sorted(names)}")
    assert not dead, 'dead byName overrides:\n' + '\n'.join(dead)


def test_no_duplicate_override_for_one_series(dashboard):
    """Two overrides on the same series: the later silently wins."""
    name, data = dashboard
    for panel in data['panels']:
        targeted = [o['matcher']['options'] for o in by_name_overrides(panel)]
        assert len(targeted) == len(set(targeted)), (
            f"{name} panel {panel['id']}: duplicate overrides for "
            f"{[n for n in targeted if targeted.count(n) > 1]}")


def test_panel_ids_are_unique(dashboard):
    """Duplicate ids make panels unlinkable and break the port comparison."""
    name, data = dashboard
    ids = [p['id'] for p in data['panels']]
    assert len(ids) == len(set(ids)), f"{name}: duplicate panel ids"


def test_target_refids_are_unique_within_a_panel(dashboard):
    """Grafana keys results by refId; a collision drops one series."""
    name, data = dashboard
    for panel in data['panels']:
        refs = [t.get('refId') for t in panel.get('targets', [])]
        assert len(refs) == len(set(refs)), (
            f"{name} panel {panel['id']}: duplicate refIds {refs}")


def test_uids_are_unique_across_dashboards():
    """All four are provisioned from one directory; a shared uid overwrites."""
    seen = {}
    for name in ALL_DASHBOARDS:
        uid = load(name)['uid']
        assert uid not in seen, f"{name} shares uid {uid!r} with {seen[uid]}"
        seen[uid] = name


def test_every_panel_has_a_datasource(dashboard):
    name, data = dashboard
    for panel in data['panels']:
        assert panel.get('datasource'), f"{name} panel {panel['id']}"
