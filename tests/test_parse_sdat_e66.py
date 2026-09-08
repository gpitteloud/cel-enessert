"""Tests for parse_sdat_e66_individual (ValidatedMeteredData_1.6)."""
import pytest

from scripts.models import MeteredData, MetricType, SkippedDocument
from scripts.parse_sdat import parse_sdat
from conftest import make_e66_xml, meter_id, real_files, SAMPLE_METERS
from scripts.meters import Meters


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
    # An undeclared meter's own total is stored under that meter, not attributed.
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
# Production breakdown attribution, from the declared meters
# --------------------------------------------------------------------------

def test_production_meter_attributed_to_its_consumption_twin(write_xml):
    # A production metering point's breakdown is stored under the consumption
    # meter of the same member, so meter_id is already that one on return.
    f = write_xml(make_e66_xml(point="production", meter_id=meter_id("0855229G"),
                               product_code="2404050010123"))
    r = parse_sdat(f, SAMPLE_METERS)
    assert r.meter_id == meter_id("0020576V")
    assert r.metric_type == MetricType.PRODUCTION_LOCAL


def test_a_lone_file_is_attributed_without_its_delivery(write_xml):
    """The case discovery could not handle: pairing needed the consumption
    meter's total in the same batch, so a file arriving late -- or retried on its
    own -- was unattributable. A lookup does not care."""
    f = write_xml(make_e66_xml(point="production", meter_id=meter_id("08552310"),
                               product_code="2404050010123"))
    assert parse_sdat(f, SAMPLE_METERS).meter_id == meter_id("0046782G")


def test_production_only_meter_attributed_to_itself(write_xml):
    # Declared production-only: it has no consumption twin, so it owns its
    # breakdown and nothing is re-attributed.
    mid = meter_id("0134575W")
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="2404050010123"))
    assert parse_sdat(f, SAMPLE_METERS).meter_id == mid


def test_paired_production_meters_total_dropped(write_xml):
    # A paired production meter's ebIX production TOTAL duplicates its
    # consumption meter's total, so it must be dropped to avoid double counting
    # the community production sum. The drop is signalled as a SkippedDocument
    # (not None) so callers can log it as expected rather than as a failure.
    production = meter_id("0855229G")
    f = write_xml(make_e66_xml(point="production", meter_id=production,
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, SAMPLE_METERS)
    assert isinstance(r, SkippedDocument)
    assert r.meter_id == production
    assert meter_id("0020576V") in r.reason


def test_production_only_meters_total_kept(write_xml):
    # It has no twin reporting the same total, so dropping it would lose that
    # member's production entirely.
    mid = meter_id("0134575W")
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, SAMPLE_METERS)
    assert r.metric_type == MetricType.PRODUCTION_TOTAL
    assert r.meter_id == mid


def test_consumption_meters_own_production_total_kept(write_xml):
    # The consumption side of a pair reports the canonical production total.
    f = write_xml(make_e66_xml(point="production", meter_id=meter_id("0020576V"),
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, SAMPLE_METERS)
    assert r.metric_type == MetricType.PRODUCTION_TOTAL
    assert r.meter_id == meter_id("0020576V")


def test_a_consumption_file_on_a_production_meter_is_skipped(write_xml):
    """The provider sends consumption files for 0134575W, which is a production
    metering point and has no consumption. Ingesting them added ~635 kWh of
    consumption that no member drew, so they are dropped -- deliberately, hence
    a SkippedDocument and not a failure."""
    mid = meter_id("0134575W")
    f = write_xml(make_e66_xml(point="consumption", meter_id=mid,
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, SAMPLE_METERS)
    assert isinstance(r, SkippedDocument)
    assert r.meter_id == mid


def test_a_consumption_file_on_a_declared_member_is_ingested(write_xml):
    """The consumption side of a pair legitimately reports consumption, so the
    rule above must key on the metering point's role and nothing wider."""
    mid = meter_id("0020576V")
    f = write_xml(make_e66_xml(point="consumption", meter_id=mid))
    assert parse_sdat(f, SAMPLE_METERS).meter_id == mid


def test_undeclared_meters_breakdown_returns_none(write_xml):
    # A production breakdown from a meter nobody declared IS a failure (a new
    # member needs the provider), so it must stay None -- never a
    # SkippedDocument, which would silence it and archive the file.
    f = write_xml(make_e66_xml(point="production", meter_id=meter_id("0999999X"),
                               product_code="2404050010123"))
    assert parse_sdat(f, SAMPLE_METERS) is None


def test_a_consumption_only_members_breakdown_returns_none(caplog):
    """A declaration error, not an unknown meter: it gets its own message so the
    question to the provider is the right one."""
    import logging
    from scripts.parse_sdat import parse_sdat_bytes
    mid = meter_id("0036273C")
    xml = make_e66_xml(point="production", meter_id=mid,
                       product_code="2404050010123").encode()
    with caplog.at_level(logging.ERROR):
        assert parse_sdat_bytes(xml, "doc.xml", SAMPLE_METERS) is None
    assert "consumption-only" in caplog.text


def test_without_a_declaration_no_breakdown_is_attributed(write_xml):
    """An empty declaration attributes nothing rather than falling back to the
    file's own meter, which would store one member's production under another."""
    f = write_xml(make_e66_xml(point="production", meter_id=meter_id("0855229G"),
                               product_code="2404050010123"))
    assert parse_sdat(f, Meters.empty()) is None


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
    skipped -- never a hard failure (None). The legitimate skips are a paired
    production meter's ebIX production TOTAL (identical to its consumption
    meter's) and a consumption file sent for a production metering point."""
    parsed = 0
    dropped = 0
    for f in _E66_SAMPLES:
        r = parse_sdat(f, SAMPLE_METERS)
        assert r is not None, f"{f.name}: unexpected parse failure"
        if isinstance(r, SkippedDocument):
            # Only a declared production metering point may be skipped: a paired
            # one's duplicate total, or a production-only one's spurious
            # consumption file.
            assert SAMPLE_METERS.is_production_metering_point(r.meter_id), \
                f"unexpected skip of {f.name} (not a production metering point)"
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
    assert dropped > 0, "expected some duplicate production totals to be dropped"


@pytest.mark.skipif(not _E66_SAMPLES, reason="no real E66 sample files present")
def test_real_e66_product_codes_are_known():
    """Real files only carry the three product codes the parser handles."""
    known = {"8716867000030", "2404050010123", "2404050010124"}
    seen = set()
    for f in _E66_SAMPLES:
        r = parse_sdat(f, SAMPLE_METERS)
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
    r = parse_sdat(f, SAMPLE_METERS)
    rows = rows_from_e66(r)
    assert len(rows) == len(r.observations)
    row = dict(zip(E66_COLUMNS, rows[0]))
    assert row["meter_id"]
    assert row["direction"] in ("consumption", "production")
    assert row["segment"] in ("cel", "grid", "total")
