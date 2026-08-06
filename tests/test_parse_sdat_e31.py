"""Tests for parse_sdat_e31_aggregated (AggregatedMeteredData_1.3)."""
import pytest

from parse_sdat import parse_sdat
from models import MetricType
from conftest import make_e31_xml, real_files


# --------------------------------------------------------------------------
# parse_e31 - metadata extraction
# --------------------------------------------------------------------------

def test_parse_basic_metadata(write_xml):
    f = write_xml(make_e31_xml(flow="E17", product_code="2404050010123"))
    r = parse_sdat(f)
    assert r.document_type == "E31"
    assert r.community_id == "101110-002726"
    assert r.community_type == "CT01"
    assert r.product_code == "2404050010123"
    assert r.code_type == "VSENationalCode"
    assert r.flow_characteristic == "E17"
    assert r.grid_area == "12Y-0000000719-J"
    assert r.resolution_minutes == 15


def test_flow_e18_production(write_xml):
    f = write_xml(make_e31_xml(flow="E18"))
    r = parse_sdat(f)
    assert r.flow_characteristic == "E18"


# --------------------------------------------------------------------------
# metric_type classification (shared scheme with E66)
# --------------------------------------------------------------------------

def test_metric_type_consumption_local(write_xml):
    # E17 (consumption) + VSE local code -> CONSUMPTION_LOCAL
    f = write_xml(make_e31_xml(flow="E17", product_code="2404050010123"))
    assert parse_sdat(f).metric_type == MetricType.CONSUMPTION_LOCAL


def test_metric_type_production_local(write_xml):
    # E18 (production) + VSE local code -> PRODUCTION_LOCAL
    f = write_xml(make_e31_xml(flow="E18", product_code="2404050010123"))
    assert parse_sdat(f).metric_type == MetricType.PRODUCTION_LOCAL


def test_metric_type_consumption_grid(write_xml):
    f = write_xml(make_e31_xml(flow="E17", product_code="2404050010124"))
    assert parse_sdat(f).metric_type == MetricType.CONSUMPTION_GRID


def test_metric_type_production_total_ebix(write_xml):
    f = write_xml(make_e31_xml(flow="E18", product_code="8716867000030",
                               code_type="ebIXCode"))
    assert parse_sdat(f).metric_type == MetricType.PRODUCTION_TOTAL


def test_metric_type_none_for_unknown_flow(write_xml):
    # A flow that is neither E17 nor E18 cannot be classified
    f = write_xml(make_e31_xml(flow="E99", product_code="2404050010123"))
    assert parse_sdat(f).metric_type is None


def test_unknown_flow_yields_no_rows(write_xml):
    # No classification -> no direction/segment to store, so the writer emits
    # nothing rather than inventing an 'unknown' row that a sum() would pick up.
    from questdb_writer import rows_from_e31
    f = write_xml(make_e31_xml(flow="E99", product_code="2404050010123"))
    assert rows_from_e31(parse_sdat(f)) == []


def test_metric_type_direction_segment_properties():
    # The enum splits into the two stored columns; 'local' maps to 'cel'
    assert MetricType.CONSUMPTION_LOCAL.direction == "consumption"
    assert MetricType.CONSUMPTION_LOCAL.segment == "cel"
    assert MetricType.PRODUCTION_GRID.direction == "production"
    assert MetricType.PRODUCTION_GRID.segment == "grid"
    assert MetricType.CONSUMPTION_TOTAL.segment == "total"


def test_ebix_product_code(write_xml):
    f = write_xml(make_e31_xml(product_code="8716867000030", code_type="ebIXCode"))
    r = parse_sdat(f)
    assert r.product_code == "8716867000030"
    assert r.code_type == "ebIXCode"


# --------------------------------------------------------------------------
# Observations & timestamps
# --------------------------------------------------------------------------

def test_observations_with_timestamps(write_xml):
    f = write_xml(make_e31_xml(values=(10.0, 20.0, 30.0),
                               start="2026-06-10T22:00:00Z",
                               resolution=15))
    r = parse_sdat(f)
    obs = r.observations
    assert len(obs) == 3
    assert obs[0].sequence == 1
    assert obs[0].value == 10.0
    assert obs[0].timestamp.startswith("2026-06-10T22:00:00")
    assert "22:30:00" in obs[2].timestamp
    # condition flag captured
    assert obs[0].condition == "21"


def test_observation_count(write_xml):
    f = write_xml(make_e31_xml(values=tuple(float(i) for i in range(96))))
    r = parse_sdat(f)
    assert len(r.observations) == 96


# --------------------------------------------------------------------------
# Rejection / edge cases
# --------------------------------------------------------------------------

def test_unknown_document_type_returns_none(write_xml):
    # DocumentType that is neither E66 nor E31 -> parse_sdat refuses to dispatch
    f = write_xml(make_e31_xml(doc_type="E99"))
    assert parse_sdat(f) is None


def test_missing_document_type_returns_none(write_xml):
    # No DocumentType at all -> cannot dispatch
    f = write_xml(make_e31_xml(doc_type=None))
    assert parse_sdat(f) is None


def test_no_metering_data_returns_none(write_xml):
    f = write_xml(make_e31_xml(include_metering_data=False))
    assert parse_sdat(f) is None


def test_missing_start_datetime_returns_none(write_xml):
    # Without StartDateTime the parser cannot compute timestamps -> reject the
    # document (same policy as E66) rather than emit an untimestamped result.
    f = write_xml(make_e31_xml(include_start=False))
    assert parse_sdat(f) is None


def test_malformed_xml_returns_none(write_xml):
    f = write_xml("<rsm:AggregatedMeteredData_13><broken>", name="bad.xml")
    # parser catches exceptions and returns None
    assert parse_sdat(f) is None


# --------------------------------------------------------------------------
# Golden-file tests against real sample data.
# Skip automatically when input/all/ is absent (gitignored).
# --------------------------------------------------------------------------

_E31_SAMPLES = real_files("*_E31_*.xml")


@pytest.mark.skipif(not _E31_SAMPLES, reason="no real E31 sample files present")
def test_real_e31_files_all_parse():
    """Every real E31 file must parse to a MeteredData with expected shape."""
    for f in _E31_SAMPLES:
        r = parse_sdat(f)
        assert r is not None, f"failed to parse real file: {f.name}"
        assert r.document_type == "E31"
        assert r.community_id
        assert r.flow_characteristic in ("E17", "E18")
        assert r.resolution_minutes == 15
        # 15-min resolution over whole days => multiple of 96
        # (real deliveries seen: 480 = 5 days, 2976 = 31 days)
        assert r.observations, f"no observations in {f.name}"
        assert len(r.observations) % 96 == 0, f"{f.name}: {len(r.observations)} obs"


@pytest.mark.skipif(not _E31_SAMPLES, reason="no real E31 sample files present")
def test_real_e31_flows_and_codes():
    """Real E31 data carries both flows and only known product codes."""
    known = {"8716867000030", "2404050010123", "2404050010124"}
    flows = set()
    codes = set()
    for f in _E31_SAMPLES:
        r = parse_sdat(f)
        if r:
            flows.add(r.flow_characteristic)
            codes.add(r.product_code)
    assert flows == {"E17", "E18"}, f"expected both flows, got {flows}"
    assert not (codes - known), f"unexpected product codes: {codes - known}"


@pytest.mark.skipif(not _E31_SAMPLES, reason="no real E31 sample files present")
def test_real_e31_builds_storable_rows():
    from questdb_writer import E31_COLUMNS, rows_from_e31
    r = parse_sdat(_E31_SAMPLES[0])
    rows = rows_from_e31(r)
    assert len(rows) == len(r.observations)
    row = dict(zip(E31_COLUMNS, rows[0]))
    assert row["community_id"]
    assert row["direction"] in ("consumption", "production")
    assert row["segment"] in ("cel", "grid", "total")
