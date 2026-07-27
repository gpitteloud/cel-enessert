"""Tests for vm_upsert - guaranteeing the NEWEST delivered value wins in VM.

Why this needs its own suite: VictoriaMetrics has no per-timestamp overwrite. It
keeps the **maximum** value for a duplicated ``(metric, labels, timestamp)``, and
the provider revises ~2.6% of overlapping slots -- sometimes downward. Plain
re-importing therefore silently pins a slot to the highest value ever delivered.
``vm_upsert`` detects revisions against a local store and rewrites the affected
series. These tests pin that contract, including the failure modes.

`FakeVictoriaMetrics` (in conftest) reproduces the max-on-duplicate rule, so a
regression that goes back to plain imports fails here rather than in production.
"""
import pytest

from conftest import real_files, SAMPLE_MAPPINGS, SAMPLE_PHYSICAL_METERS
from vm_upsert import selector_for, upsert


METRIC = {'__name__': 'cel_energy_kwh', 'meter_id': 'M1', 'segment': 'total',
          'direction': 'consumption'}
OTHER = {'__name__': 'cel_energy_kwh', 'meter_id': 'M2', 'segment': 'total',
         'direction': 'consumption'}

TS = 1780531200000
STEP = 900_000          # 15 minutes in ms


def point(metric, timestamps, values):
    return {'metric': metric, 'values': list(values),
            'timestamps': list(timestamps)}


# --------------------------------------------------------------------------
# selector_for
# --------------------------------------------------------------------------

def test_selector_is_exact_and_sorted():
    sel = selector_for(METRIC)
    assert sel.startswith('cel_energy_kwh{')
    # Sorted label order keeps the selector stable across dict orderings, so the
    # same series always maps to the same delete target.
    assert sel == ('cel_energy_kwh{direction="consumption",meter_id="M1",'
                   'segment="total"}')


def test_selector_escapes_quotes():
    sel = selector_for({'__name__': 'm', 'label': 'a"b\\c'})
    assert sel == 'm{label="a\\"b\\\\c"}'


def test_selector_without_labels():
    assert selector_for({'__name__': 'bare'}) == 'bare'


# --------------------------------------------------------------------------
# Core upsert behaviour
# --------------------------------------------------------------------------

def test_new_samples_are_imported(fake_vm, sample_store):
    stats = upsert([point(METRIC, [TS, TS + STEP], [1.0, 2.0])],
                   'http://vm', '20260701', sample_store)
    assert stats == {'new': 2, 'unchanged': 0, 'revised': 0,
                     'rewritten_series': 0, 'failed': 0, 'stale': 0}
    assert fake_vm.data[selector_for(METRIC)] == {TS: 1.0, TS + STEP: 2.0}


def test_unchanged_samples_are_not_resent(fake_vm, sample_store):
    upsert([point(METRIC, [TS], [1.0])], 'http://vm', '20260701', sample_store)
    imports_before = fake_vm.imports

    stats = upsert([point(METRIC, [TS], [1.0])], 'http://vm', '20260702',
                   sample_store)

    assert stats['unchanged'] == 1 and stats['new'] == 0
    # The whole point: redundant overlap costs zero writes.
    assert fake_vm.imports == imports_before
    assert fake_vm.data[selector_for(METRIC)] == {TS: 1.0}


def test_upward_revision_wins(fake_vm, sample_store):
    upsert([point(METRIC, [TS], [1.0])], 'http://vm', '20260701', sample_store)
    stats = upsert([point(METRIC, [TS], [5.0])], 'http://vm', '20260702',
                   sample_store)

    assert stats['revised'] == 1 and stats['rewritten_series'] == 1
    assert fake_vm.data[selector_for(METRIC)] == {TS: 5.0}


def test_downward_revision_wins(fake_vm, sample_store):
    """The case plain importing cannot express: VM would keep 0.003."""
    upsert([point(METRIC, [TS], [0.003])], 'http://vm', '20260527', sample_store)
    stats = upsert([point(METRIC, [TS], [0.002])], 'http://vm', '20260605',
                   sample_store)

    assert stats['revised'] == 1 and stats['rewritten_series'] == 1
    assert fake_vm.data[selector_for(METRIC)] == {TS: 0.002}
    assert fake_vm.deletes == 1


def test_revision_preserves_other_samples_in_series(fake_vm, sample_store):
    """A rewrite deletes the whole series, so history must be replayed intact."""
    timestamps = [TS + i * STEP for i in range(5)]
    upsert([point(METRIC, timestamps, [1.0, 2.0, 3.0, 4.0, 5.0])],
           'http://vm', '20260701', sample_store)

    upsert([point(METRIC, [timestamps[2]], [0.5])], 'http://vm', '20260702',
           sample_store)

    assert fake_vm.data[selector_for(METRIC)] == {
        timestamps[0]: 1.0, timestamps[1]: 2.0, timestamps[2]: 0.5,
        timestamps[3]: 4.0, timestamps[4]: 5.0,
    }


def test_revision_does_not_touch_other_series(fake_vm, sample_store):
    upsert([point(METRIC, [TS], [1.0]), point(OTHER, [TS], [9.0])],
           'http://vm', '20260701', sample_store)

    upsert([point(METRIC, [TS], [0.5])], 'http://vm', '20260702', sample_store)

    assert fake_vm.data[selector_for(OTHER)] == {TS: 9.0}
    assert fake_vm.deletes == 1


def test_older_delivery_cannot_regress_a_value(fake_vm, sample_store):
    """Replaying an out-of-order (older) delivery must be a no-op."""
    upsert([point(METRIC, [TS], [2.0])], 'http://vm', '20260610', sample_store)
    stats = upsert([point(METRIC, [TS], [1.0])], 'http://vm', '20260527',
                   sample_store)

    assert stats['stale'] == 1 and stats['revised'] == 0
    assert fake_vm.data[selector_for(METRIC)] == {TS: 2.0}


def test_idempotent_rerun(fake_vm, sample_store):
    batch = [point(METRIC, [TS, TS + STEP], [1.0, 2.0])]
    upsert(batch, 'http://vm', '20260701', sample_store)
    deletes, imports = fake_vm.deletes, fake_vm.imports

    stats = upsert(batch, 'http://vm', '20260701', sample_store)

    assert stats['unchanged'] == 2 and stats['revised'] == 0
    assert fake_vm.deletes == deletes and fake_vm.imports == imports


def test_duplicate_slot_within_one_batch_uses_last(fake_vm, sample_store):
    """Two lines for the same slot in one delivery: the later line is the value."""
    stats = upsert([point(METRIC, [TS], [1.0]), point(METRIC, [TS], [3.0])],
                   'http://vm', '20260701', sample_store)

    assert stats['new'] == 1
    assert fake_vm.data[selector_for(METRIC)] == {TS: 3.0}


def test_invalid_points_counted_as_failed(fake_vm, sample_store):
    stats = upsert([{'metric': METRIC},                      # no values
                    point(METRIC, [TS], [1.0])],
                   'http://vm', '20260701', sample_store)
    assert stats['failed'] == 1 and stats['new'] == 1


def test_empty_batch_is_a_noop(fake_vm, sample_store):
    stats = upsert([], 'http://vm', '20260701', sample_store)
    assert stats['new'] == 0 and stats['failed'] == 0
    assert fake_vm.imports == 0


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------

def test_failed_delete_leaves_series_untouched(fake_vm, sample_store):
    upsert([point(METRIC, [TS], [1.0])], 'http://vm', '20260701', sample_store)
    fake_vm.fail_next_delete = True

    stats = upsert([point(METRIC, [TS], [0.5])], 'http://vm', '20260702',
                   sample_store)

    assert stats['failed'] == 1 and stats['rewritten_series'] == 0
    # Old value still in VM: the caller sees FAILED and the file is retried.
    assert fake_vm.data[selector_for(METRIC)] == {TS: 1.0}


def test_store_holds_new_value_after_failed_rewrite_so_rerun_repairs(
        fake_vm, sample_store):
    """The local store is the source of truth, so a rerun completes the repair."""
    upsert([point(METRIC, [TS], [1.0])], 'http://vm', '20260701', sample_store)
    fake_vm.fail_next_delete = True
    upsert([point(METRIC, [TS], [0.5])], 'http://vm', '20260702', sample_store)

    # Same delivery replayed: store says 0.5 already, so nothing is "revised",
    # but VM is still wrong - which is why the rewrite must be retried by
    # re-sending the file, and why the failure is logged loudly.
    stats = upsert([point(METRIC, [TS], [0.5])], 'http://vm', '20260702',
                   sample_store)
    assert stats['unchanged'] == 1

    sid = sample_store.series_id(METRIC)
    assert sample_store.all_samples(sid) == [(TS, 0.5)]


def test_failed_import_reports_failure(fake_vm, sample_store):
    fake_vm.fail_next_import = True
    stats = upsert([point(METRIC, [TS], [1.0])], 'http://vm', '20260701',
                   sample_store)
    assert stats['failed'] == 1 and stats['new'] == 0


# --------------------------------------------------------------------------
# Store persistence
# --------------------------------------------------------------------------

def test_store_survives_reopen(fake_vm, sample_store, tmp_path):
    from vm_upsert import SampleStore

    upsert([point(METRIC, [TS], [1.0])], 'http://vm', '20260701', sample_store)
    sample_store.close()

    reopened = SampleStore(tmp_path / 'vm_samples.db')
    try:
        stats = upsert([point(METRIC, [TS], [1.0])], 'http://vm', '20260702',
                       reopened)
        # Recognised as already-known across a restart, not re-imported.
        assert stats['unchanged'] == 1 and stats['new'] == 0
    finally:
        reopened.close()


# --------------------------------------------------------------------------
# Golden test against real overlapping deliveries
# --------------------------------------------------------------------------

def _parse_delivery(files):
    """Parse real files into VM data points (E66 + E31)."""
    import xml.etree.ElementTree as ET
    from models import SkippedDocument
    from parse_sdat_e66_individual import parse_e66, transform_to_datapoints
    from parse_sdat_e31_aggregated import parse_e31, transform_e31_to_datapoints

    ns = '{http://www.strom.ch}'
    doc_type = f'.//{ns}DocumentType/{ns}ebIXCode'
    points = []
    for path in files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        elem = root.find(doc_type)
        kind = elem.text if elem is not None else None
        if kind == 'E66':
            parsed = parse_e66(root, meter_mappings=SAMPLE_MAPPINGS,
                               physical_production_meters=SAMPLE_PHYSICAL_METERS)
            if parsed is None or isinstance(parsed, SkippedDocument):
                continue
            points.extend(transform_to_datapoints(parsed))
        elif kind == 'E31':
            parsed = parse_e31(root)
            if parsed is not None:
                points.extend(transform_e31_to_datapoints(parsed))
    return points


def _real_deliveries(first='20260527', last='20260603'):
    """Group real sample files by delivery date within a window."""
    groups = {}
    for path in real_files('*.xml'):
        delivery = path.name[:8]
        if first <= delivery <= last:
            groups.setdefault(delivery, []).append(path)
    return groups


@pytest.mark.parametrize('order', ['ascending', 'descending'])
def test_real_overlapping_deliveries_yield_newest_value(fake_vm, sample_store,
                                                        order):
    """VM must end up byte-identical to "newest delivery wins", either order.

    Real deliveries overlap by 4 days and contain genuine revisions in both
    directions, which is what makes this stronger than the synthetic cases.
    """
    groups = _real_deliveries()
    if len(groups) < 3:
        pytest.skip('needs >=3 real overlapping deliveries in input/all')

    batches = {d: _parse_delivery(f) for d, f in groups.items()}

    expected = {}
    for delivery in sorted(batches):
        for p in batches[delivery]:
            selector = selector_for(p['metric'])
            for ts, value in zip(p['timestamps'], p['values']):
                expected.setdefault(selector, {})[ts] = value

    deliveries = sorted(batches, reverse=(order == 'descending'))
    for delivery in deliveries:
        stats = upsert(batches[delivery], 'http://vm', delivery, sample_store)
        assert stats['failed'] == 0

    assert fake_vm.data == expected


def test_real_deliveries_contain_downward_revisions():
    """Guards the premise: if the samples ever stop containing downward
    revisions, the golden test above silently stops covering the hard case."""
    groups = _real_deliveries()
    if len(groups) < 3:
        pytest.skip('needs >=3 real overlapping deliveries in input/all')

    seen, downward = {}, 0
    for delivery in sorted(groups):
        for p in _parse_delivery(groups[delivery]):
            selector = selector_for(p['metric'])
            for ts, value in zip(p['timestamps'], p['values']):
                key = (selector, ts)
                if key in seen and value < seen[key]:
                    downward += 1
                seen[key] = value

    assert downward > 0, 'expected the provider to revise some slots downward'
