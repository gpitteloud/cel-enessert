"""Tests for parse_sdat_e66_individual (ValidatedMeteredData_1.6)."""
import pytest

from scripts.models import MeteredData, MetricType, SkippedDocument
from scripts.parse_sdat import parse_sdat
from conftest import (
    make_e66_xml,
    meter_id,
    real_files,
    SAMPLE_MAPPINGS,
    SAMPLE_SELF_CONTAINED,
)


# --------------------------------------------------------------------------
# parse_e66 - metering point + metric type classification
# --------------------------------------------------------------------------

def test_consumption_local_vse(write_xml):
    f = write_xml(make_e66_xml(point="consumption",
                               product_code="2404050010123",
                               code_type="VSENationalCode"))
    r = parse_sdat(f)
    assert r.document_type == "E66"
    assert r.metric_type == MetricType.CONSUMPTION_LOCAL
    assert r.meter_id == meter_id("0020576V")
    assert r.community_id == "101110-002726"


def test_consumption_grid_vse(write_xml):
    f = write_xml(make_e66_xml(point="consumption", product_code="2404050010124"))
    r = parse_sdat(f)
    assert r.metric_type == MetricType.CONSUMPTION_GRID


def test_consumption_total_ebix(write_xml):
    f = write_xml(make_e66_xml(point="consumption",
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f)
    assert r.metric_type == MetricType.CONSUMPTION_TOTAL
    assert r.code_type == "ebIXCode"


def test_production_total_ebix(write_xml):
    f = write_xml(make_e66_xml(point="production",
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f)
    assert r.metric_type == MetricType.PRODUCTION_TOTAL
    # An unmapped meter's own total is stored under that meter, not attributed.
    assert r.meter_id == meter_id("0020576V")


# --------------------------------------------------------------------------
# Observations & timestamps
# --------------------------------------------------------------------------

def test_observations_parsed_with_timestamps(write_xml):
    f = write_xml(make_e66_xml(values=(1.5, 2.5, 3.5),
                               start="2026-05-21T22:00:00Z",
                               resolution=15))
    r = parse_sdat(f)
    obs = r.observations
    assert len(obs) == 3
    assert obs[0].sequence == 1
    assert obs[0].value == 1.5
    # first observation is at start time
    assert obs[0].timestamp.startswith("2026-05-21T22:00:00")
    # third is start + 2*15min = 22:30
    assert "22:30:00" in obs[2].timestamp


def test_missing_resolution_returns_none(write_xml):
    # Parser now rejects files without a resolution (returns None)
    f = write_xml(make_e66_xml(include_resolution=False))
    assert parse_sdat(f) is None


# --------------------------------------------------------------------------
# Virtual / self-contained production breakdown attribution
# --------------------------------------------------------------------------

def test_virtual_meter_mapped_to_physical(write_xml):
    # A virtual meter's production breakdown is stored under its physical twin,
    # so meter_id is already the physical one when the parser returns.
    virt = meter_id("0855229G")
    f = write_xml(make_e66_xml(point="production", meter_id=virt,
                               product_code="2404050010123"))
    r = parse_sdat(f, meter_mappings={virt: meter_id("0020576V")})
    assert r.meter_id == meter_id("0020576V")
    assert r.metric_type == MetricType.PRODUCTION_LOCAL


def test_self_contained_meter_attributed_to_itself(write_xml):
    # Production VSE breakdown on a meter that also reports consumption: it owns
    # its breakdown, so nothing is re-attributed.
    mid = meter_id("0134575W")
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="2404050010123"))
    r = parse_sdat(f, meter_mappings={}, self_contained_meters={mid})
    assert r.meter_id == mid


def test_virtual_meter_production_total_dropped(write_xml):
    # A mapped virtual meter's ebIX production TOTAL duplicates its physical
    # meter's total, so it must be dropped to avoid double counting the
    # community production sum. The drop is signalled as a SkippedDocument
    # (not None) so callers can log it as expected rather than as a failure.
    virt = meter_id("0855229G")
    f = write_xml(make_e66_xml(point="production", meter_id=virt,
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, meter_mappings={virt: meter_id("0020576V")})
    assert isinstance(r, SkippedDocument)
    assert r.meter_id == virt
    assert meter_id("0020576V") in r.reason


def test_self_contained_meter_production_total_kept(write_xml):
    # The self-contained meter is NOT in meter_mappings (only in the physical
    # set), so its own production total must be kept, not dropped.
    mid = meter_id("0134575W")
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, meter_mappings=SAMPLE_MAPPINGS,
                       self_contained_meters={mid})
    assert r is not None
    assert r.metric_type == MetricType.PRODUCTION_TOTAL
    assert r.meter_id == mid


def test_physical_meter_production_total_kept(write_xml):
    # A physical producer's own ebIX production total is always kept.
    mid = meter_id("0020576V")
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, meter_mappings=SAMPLE_MAPPINGS)
    assert r is not None
    assert r.metric_type == MetricType.PRODUCTION_TOTAL


def test_unknown_virtual_meter_returns_none(write_xml):
    # Production VSE breakdown, unknown meter, no mapping, not self-contained.
    # This IS a failure (a new member needs discovery), so it must stay None --
    # never a SkippedDocument, which would silence it and archive the file.
    mid = meter_id("0999999X")
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="2404050010123"))
    r = parse_sdat(f, meter_mappings={}, self_contained_meters=set())
    assert r is None


def test_mapping_takes_precedence_over_self_contained(write_xml):
    # If a meter is BOTH mapped and self-contained, the mapping wins
    mid = meter_id("0855229G")
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="2404050010123"))
    r = parse_sdat(f, meter_mappings={mid: meter_id("0020576V")},
                       self_contained_meters={mid})
    assert r.meter_id == meter_id("0020576V")


# --------------------------------------------------------------------------
# Malformed / edge inputs
# --------------------------------------------------------------------------

def test_no_metering_data_returns_none(write_xml):
    f = write_xml(make_e66_xml(include_metering_data=False))
    assert parse_sdat(f) is None


def test_malformed_xml_returns_none(write_xml):
    # parse_sdat catches XML parse errors and returns None (clean skip)
    f = write_xml("<rsm:ValidatedMeteredData_16><broken>", name="bad.xml")
    assert parse_sdat(f) is None


def test_no_product_code_still_parses_observations(write_xml):
    f = write_xml(make_e66_xml(product_code=None))
    r = parse_sdat(f)
    # no product -> no metric_type, but observations still extracted
    assert r.metric_type is None
    assert len(r.observations) == 3


# --------------------------------------------------------------------------
# Golden-file tests against real sample data.
# These skip automatically when input/all/ is absent (gitignored), so they
# run on machines/CI that have the real deliveries but never break elsewhere.
# --------------------------------------------------------------------------

_E66_SAMPLES = real_files("*_E66_*.xml")


@pytest.mark.skipif(not _E66_SAMPLES, reason="no real E66 sample files present")
def test_real_e66_files_all_parse():
    """Every real E66 file either parses to a MeteredData or is deliberately
    skipped -- never a hard failure (None). The only legitimate skip is a mapped
    virtual meter's ebIX production TOTAL (identical to its physical meter's)."""
    parsed = 0
    dropped = 0
    for f in _E66_SAMPLES:
        r = parse_sdat(f, meter_mappings=SAMPLE_MAPPINGS,
                           self_contained_meters=SAMPLE_SELF_CONTAINED)
        assert r is not None, f"{f.name}: unexpected parse failure"
        if isinstance(r, SkippedDocument):
            # Only a mapped virtual meter's production total may be skipped.
            assert r.meter_id in SAMPLE_MAPPINGS, \
                f"unexpected skip of {f.name} (not a mapped virtual meter)"
            dropped += 1
            continue
        assert r.document_type == "E66"
        # 15-min resolution over whole days => observation count is a multiple
        # of 96 (real deliveries seen: 480 = 5 days, 2976 = 31 days)
        assert r.observations, f"no observations in {f.name}"
        assert len(r.observations) % 96 == 0, f"{f.name}: {len(r.observations)} obs"
        assert r.community_id  # present
        parsed += 1
    assert parsed + dropped == len(_E66_SAMPLES)
    assert dropped > 0, "expected some virtual production totals to be dropped"


@pytest.mark.skipif(not _E66_SAMPLES, reason="no real E66 sample files present")
def test_real_e66_product_codes_are_known():
    """Real files only carry the three product codes the parser handles."""
    known = {"8716867000030", "2404050010123", "2404050010124"}
    seen = set()
    for f in _E66_SAMPLES:
        r = parse_sdat(f, meter_mappings=SAMPLE_MAPPINGS,
                           self_contained_meters=SAMPLE_SELF_CONTAINED)
        if isinstance(r, MeteredData) and r.product_code:
            seen.add(r.product_code)
    assert seen, "no product codes seen"
    unexpected = seen - known
    assert not unexpected, f"unexpected product codes in real data: {unexpected}"


@pytest.mark.skipif(not _E66_SAMPLES, reason="no real E66 sample files present")
def test_real_e66_builds_storable_rows():
    """A real file must produce one storable row per observation."""
    from scripts.questdb_writer import E66_COLUMNS, rows_from_e66
    f = _E66_SAMPLES[0]
    r = parse_sdat(f, meter_mappings=SAMPLE_MAPPINGS,
                       self_contained_meters=SAMPLE_SELF_CONTAINED)
    rows = rows_from_e66(r)
    assert len(rows) == len(r.observations)
    row = dict(zip(E66_COLUMNS, rows[0]))
    assert row["meter_id"]
    assert row["direction"] in ("consumption", "production")
    assert row["segment"] in ("cel", "grid", "total")
