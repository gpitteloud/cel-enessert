"""Tests for parse_sdat_e66_individual (ValidatedMeteredData_1.6)."""
import pytest

from scripts.models import MeteredData, MetricType, SkippedDocument
from scripts.parse_sdat import parse_sdat
from conftest import make_e66_xml, meter_id, real_files, SAMPLE_METERS


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
    # Without a declaration every meter is stored under itself, with no customer.
    assert r.meter_id == meter_id("0020576V")
    assert r.customer_id is None


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
# Every meter under its own id, with its customer
# --------------------------------------------------------------------------

def test_a_production_breakdown_stays_on_its_production_point(write_xml):
    f = write_xml(make_e66_xml(point="production", meter_id=meter_id("0855229G"),
                               product_code="2404050010123"))
    r = parse_sdat(f, SAMPLE_METERS)
    assert r.meter_id == meter_id("0855229G")
    assert r.customer_id == "9000107"
    assert r.metric_type == MetricType.PRODUCTION_LOCAL


def test_a_consumption_file_carries_its_customer(write_xml):
    f = write_xml(make_e66_xml(point="consumption", meter_id=meter_id("0020576V")))
    r = parse_sdat(f, SAMPLE_METERS)
    assert (r.meter_id, r.customer_id) == (meter_id("0020576V"), "9000107")


def test_the_header_records_the_customer(write_xml):
    """parse_e66 sets it on the header too, so cel_file_header carries it."""
    import xml.etree.ElementTree as ET
    from scripts.parse_sdat import metered_data_from_header
    from scripts.sdat_header import parse_header
    header = parse_header(ET.fromstring(make_e66_xml(
        point="consumption", meter_id=meter_id("0020576V"))), "doc.xml")
    metered_data_from_header(header, SAMPLE_METERS)
    assert header.customer_id == "9000107"


def test_a_production_points_total_is_kept(write_xml):
    """The production point's copy of the total is the one stored, so the meter
    is self-contained: its total is its cel + grid."""
    mid = meter_id("0855229G")
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, SAMPLE_METERS)
    assert r.metric_type == MetricType.PRODUCTION_TOTAL
    assert r.meter_id == mid


def test_an_uncoupled_production_points_total_is_kept(write_xml):
    mid = meter_id("0134575W")
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, SAMPLE_METERS)
    assert r.metric_type == MetricType.PRODUCTION_TOTAL
    assert (r.meter_id, r.customer_id) == (mid, "9000114")


def test_a_consumption_points_copy_of_the_production_total_is_dropped(write_xml):
    """It duplicates the production point's total, so storing it would double
    the community's production. A SkippedDocument (not None) lets the caller
    log it as expected, not as a failure."""
    consumption = meter_id("0020576V")
    f = write_xml(make_e66_xml(point="production", meter_id=consumption,
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, SAMPLE_METERS)
    assert isinstance(r, SkippedDocument)
    assert r.meter_id == consumption
    assert "9000107" in r.reason


def test_the_copy_is_dropped_on_any_consumption_point_of_the_customer(write_xml):
    """9000106 owns two consumption points; either may send the copy."""
    for suffix in ("02291991", "01650626"):
        f = write_xml(make_e66_xml(point="production", meter_id=meter_id(suffix),
                                   product_code="8716867000030",
                                   code_type="ebIXCode"))
        assert isinstance(parse_sdat(f, SAMPLE_METERS), SkippedDocument)


def test_a_consumption_file_on_a_production_meter_is_skipped(write_xml):
    """The provider sends consumption files for 0134575W, which is a production
    metering point and has no consumption. Ingesting them added ~635 kWh of
    consumption that no member drew, so they are dropped -- deliberately, hence
    a SkippedDocument and not a failure. Its customer's consumption point
    carries the customer's consumption."""
    mid = meter_id("0134575W")
    f = write_xml(make_e66_xml(point="consumption", meter_id=mid,
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat(f, SAMPLE_METERS)
    assert isinstance(r, SkippedDocument)
    assert r.meter_id == mid


def test_a_breakdown_against_the_meters_role_fails(caplog):
    """A meter measures only its own direction, so a CEL or grid file names its
    role. One that disagrees means customers.yaml declares the wrong role; a
    skip would archive the meter's data silently, so it fails instead -- even
    when the customer owns a meter of that direction."""
    import logging
    from scripts.parse_sdat import parse_sdat_bytes
    cases = [("consumption", meter_id("0134575W")),   # declared production
             ("production", meter_id("0020576V"))]    # declared consumption
    for point, mid in cases:
        xml = make_e66_xml(point=point, meter_id=mid,
                           product_code="2404050010123",
                           code_type="VSENationalCode").encode()
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            assert parse_sdat_bytes(xml, "doc.xml", SAMPLE_METERS) is None
        assert "role in customers.yaml" in caplog.text


def test_production_from_a_consumption_only_customer_fails(caplog):
    """Nothing else carries it -- most likely the customer installed solar before
    the list was updated -- so dropping it would lose real production. It must
    be None, never a SkippedDocument, which would archive it silently."""
    import logging
    from scripts.parse_sdat import parse_sdat_bytes
    xml = make_e66_xml(point="production", meter_id=meter_id("0036273C"),
                       product_code="8716867000030",
                       code_type="ebIXCode").encode()
    with caplog.at_level(logging.ERROR):
        assert parse_sdat_bytes(xml, "doc.xml", SAMPLE_METERS) is None
    assert "9000112" in caplog.text
    assert "customers.yaml" in caplog.text


def test_an_undeclared_meter_is_stored_with_no_customer(write_xml, caplog):
    import logging
    f = write_xml(make_e66_xml(point="production", meter_id=meter_id("0999999X"),
                               product_code="2404050010123"))
    with caplog.at_level(logging.WARNING):
        r = parse_sdat(f, SAMPLE_METERS)
    assert (r.meter_id, r.customer_id) == (meter_id("0999999X"), None)
    assert "not declared" in caplog.text


def test_an_undeclared_rcp_meter_is_stored_without_a_warning(write_xml, caplog):
    """The RCP meters are not the community's customers; that is expected."""
    import logging
    f = write_xml(make_e66_xml(meter_id=meter_id("0999999X"), community_id=None,
                               business_reason="E88", reason_code_type="ebIXCode",
                               product_code="8716867000030", code_type="ebIXCode"))
    with caplog.at_level(logging.WARNING):
        r = parse_sdat(f, SAMPLE_METERS)
    assert r.rcp and r.customer_id is None
    assert "not declared" not in caplog.text


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
    skipped -- never a hard failure (None). The legitimate skips are a file
    reporting against its meter's role while another meter of the customer
    carries that direction: a consumption point's copy of the production total,
    and a consumption file sent for a production metering point."""
    parsed = 0
    dropped = 0
    for f in _E66_SAMPLES:
        r = parse_sdat(f, SAMPLE_METERS)
        assert r is not None, f"{f.name}: unexpected parse failure"
        if isinstance(r, SkippedDocument):
            # Only a declared meter may be skipped, and only for another
            # meter of its customer.
            assert SAMPLE_METERS.customer_of(r.meter_id), \
                f"unexpected skip of {f.name} (undeclared meter)"
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
