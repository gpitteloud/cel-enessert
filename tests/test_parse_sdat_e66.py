"""Tests for parse_sdat_e66_individual (ValidatedMeteredData_1.6)."""
import pytest

from parse_sdat_e66_individual import (
    parse_sdat_xml,
    transform_to_datapoints,
    MetricType,
)
from conftest import (
    make_e66_xml,
    real_files,
    SAMPLE_MAPPINGS,
    SAMPLE_PHYSICAL_METERS,
)


# --------------------------------------------------------------------------
# parse_sdat_xml - metering point + metric type classification
# --------------------------------------------------------------------------

def test_consumption_local_vse(write_xml):
    f = write_xml(make_e66_xml(point="consumption",
                               product_code="2404050010123",
                               code_type="VSENationalCode"))
    r = parse_sdat_xml(f)
    assert r.document_type == "E66"
    assert r.metering_point_type == "consumption"
    assert r.metric_type == MetricType.CONSUMPTION_LOCAL
    assert r.meter_id == "CH101110123450000000000000020576V"
    assert r.community_id == "101110-002726"
    assert r.is_production_breakdown is False


def test_consumption_grid_vse(write_xml):
    f = write_xml(make_e66_xml(point="consumption", product_code="2404050010124"))
    r = parse_sdat_xml(f)
    assert r.metric_type == MetricType.CONSUMPTION_GRID


def test_consumption_total_ebix(write_xml):
    f = write_xml(make_e66_xml(point="consumption",
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat_xml(f)
    assert r.metric_type == MetricType.CONSUMPTION_TOTAL
    assert r.code_type == "ebIXCode"


def test_production_total_ebix(write_xml):
    f = write_xml(make_e66_xml(point="production",
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat_xml(f)
    assert r.metering_point_type == "production"
    assert r.metric_type == MetricType.PRODUCTION_TOTAL
    # ebIX production total is NOT a breakdown, must not be flagged
    assert r.is_production_breakdown is False


# --------------------------------------------------------------------------
# Observations & timestamps
# --------------------------------------------------------------------------

def test_observations_parsed_with_timestamps(write_xml):
    f = write_xml(make_e66_xml(values=(1.5, 2.5, 3.5),
                               start="2026-05-21T22:00:00Z",
                               resolution=15))
    r = parse_sdat_xml(f)
    obs = r.observations
    assert len(obs) == 3
    assert obs[0].sequence == 1
    assert obs[0].value == 1.5
    # first observation is at start time
    assert obs[0].timestamp.startswith("2026-05-21T22:00:00")
    # third is start + 2*15min = 22:30
    assert "22:30:00" in obs[2].timestamp


def test_resolution_extracted(write_xml):
    f = write_xml(make_e66_xml(resolution=30))
    r = parse_sdat_xml(f)
    assert r.resolution_minutes == 30


def test_missing_resolution_returns_none(write_xml):
    # Parser now rejects files without a resolution (returns None)
    f = write_xml(make_e66_xml(include_resolution=False))
    assert parse_sdat_xml(f) is None


# --------------------------------------------------------------------------
# Virtual / self-contained production breakdown attribution
# --------------------------------------------------------------------------

def test_virtual_meter_mapped_to_physical(write_xml):
    # 085-prefixed virtual meter with production VSE code, present in mappings
    virt = "CH1011101234500000000000000855229G"
    f = write_xml(make_e66_xml(point="production", meter_id=virt,
                               product_code="2404050010123"))
    r = parse_sdat_xml(f, meter_mappings={"0855229G": "0020576V"})
    assert r.is_production_breakdown is True
    assert r.attributed_physical_meter == "0020576V"
    assert r.metric_type == MetricType.PRODUCTION_LOCAL


def test_self_contained_meter_attributed_to_itself(write_xml):
    # Production VSE breakdown on a meter that is itself a physical prod meter
    mid = "CH101110123450000000000000134575W"
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="2404050010123"))
    r = parse_sdat_xml(f, meter_mappings={},
                       physical_production_meters={"0134575W"})
    assert r.is_production_breakdown is True
    assert r.attributed_physical_meter == "0134575W"


def test_unknown_virtual_meter_returns_none(write_xml):
    # Production VSE breakdown, unknown meter, no mapping, not self-contained
    mid = "CH101110123450000000000000999999X"
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="2404050010123"))
    r = parse_sdat_xml(f, meter_mappings={}, physical_production_meters=set())
    assert r is None


def test_mapping_takes_precedence_over_self_contained(write_xml):
    # If suffix is BOTH in mappings and physical set, the mapping wins
    mid = "CH1011101234500000000000000855229G"
    f = write_xml(make_e66_xml(point="production", meter_id=mid,
                               product_code="2404050010123"))
    r = parse_sdat_xml(f, meter_mappings={"0855229G": "0020576V"},
                       physical_production_meters={"0855229G"})
    assert r.attributed_physical_meter == "0020576V"


# --------------------------------------------------------------------------
# Malformed / edge inputs
# --------------------------------------------------------------------------

def test_no_metering_data_returns_none(write_xml):
    f = write_xml(make_e66_xml(include_metering_data=False))
    assert parse_sdat_xml(f) is None


def test_malformed_xml_raises(write_xml):
    f = write_xml("<rsm:ValidatedMeteredData_16><broken>", name="bad.xml")
    with pytest.raises(Exception):
        parse_sdat_xml(f)


def test_no_product_code_still_parses_observations(write_xml):
    f = write_xml(make_e66_xml(product_code=None))
    r = parse_sdat_xml(f)
    # no product -> no metric_type, but observations still extracted
    assert r.metric_type is None
    assert len(r.observations) == 3


# --------------------------------------------------------------------------
# transform_to_datapoints
# --------------------------------------------------------------------------

def test_transform_builds_vm_datapoints(write_xml):
    f = write_xml(make_e66_xml(point="consumption",
                               product_code="2404050010123",
                               values=(1.0, 2.0)))
    r = parse_sdat_xml(f)
    dps = transform_to_datapoints(r)
    assert len(dps) == 2
    m = dps[0]["metric"]
    assert m["__name__"] == "cel_energy_local_import_kwh"
    assert m["project"] == "cel"
    assert m["data_type"] == "consumption"
    assert m["meter_id"] == "CH101110123450000000000000020576V"
    assert dps[0]["values"] == [1.0]
    # timestamp converted to epoch millis (int)
    assert isinstance(dps[0]["timestamps"][0], int)


def test_transform_production_data_type(write_xml):
    f = write_xml(make_e66_xml(point="production",
                               product_code="8716867000030",
                               code_type="ebIXCode"))
    r = parse_sdat_xml(f)
    dps = transform_to_datapoints(r)
    assert dps[0]["metric"]["__name__"] == "cel_energy_produced_kwh"
    assert dps[0]["metric"]["data_type"] == "production"


def test_transform_breakdown_uses_attributed_meter_id(write_xml):
    virt = "CH1011101234500000000000000855229G"
    f = write_xml(make_e66_xml(point="production", meter_id=virt,
                               product_code="2404050010123"))
    r = parse_sdat_xml(f, meter_mappings={"0855229G": "0020576V"})
    physical_meter = "CH101110123450000000000000020576V"
    dps = transform_to_datapoints(r, attributed_meter_id=physical_meter)
    assert all(dp["metric"]["meter_id"] == physical_meter for dp in dps)


def test_transform_empty_when_no_observations():
    assert transform_to_datapoints(None) == []


def test_transform_empty_when_no_metric_type(write_xml):
    f = write_xml(make_e66_xml(product_code=None))
    r = parse_sdat_xml(f)
    assert transform_to_datapoints(r) == []


# --------------------------------------------------------------------------
# Golden-file tests against real sample data.
# These skip automatically when input/all/ is absent (gitignored), so they
# run on machines/CI that have the real deliveries but never break elsewhere.
# --------------------------------------------------------------------------

_E66_SAMPLES = real_files("*_E66_*.xml")


@pytest.mark.skipif(not _E66_SAMPLES, reason="no real E66 sample files present")
def test_real_e66_files_all_parse():
    """Every real E66 file must parse to a MeteredData (no crashes, no None)."""
    parsed = 0
    for f in _E66_SAMPLES:
        r = parse_sdat_xml(f, meter_mappings=SAMPLE_MAPPINGS,
                           physical_production_meters=SAMPLE_PHYSICAL_METERS)
        assert r is not None, f"failed to parse real file: {f.name}"
        assert r.document_type == "E66"
        assert r.resolution_minutes == 15
        # 15-min resolution over whole days => observation count is a multiple
        # of 96 (real deliveries seen: 480 = 5 days, 2976 = 31 days)
        assert r.observations, f"no observations in {f.name}"
        assert len(r.observations) % 96 == 0, f"{f.name}: {len(r.observations)} obs"
        assert r.community_id  # present
        parsed += 1
    assert parsed == len(_E66_SAMPLES)


@pytest.mark.skipif(not _E66_SAMPLES, reason="no real E66 sample files present")
def test_real_e66_product_codes_are_known():
    """Real files only carry the three product codes the parser handles."""
    known = {"8716867000030", "2404050010123", "2404050010124"}
    seen = set()
    for f in _E66_SAMPLES:
        r = parse_sdat_xml(f, meter_mappings=SAMPLE_MAPPINGS,
                           physical_production_meters=SAMPLE_PHYSICAL_METERS)
        if r and r.product_code:
            seen.add(r.product_code)
    assert seen, "no product codes seen"
    unexpected = seen - known
    assert not unexpected, f"unexpected product codes in real data: {unexpected}"


@pytest.mark.skipif(not _E66_SAMPLES, reason="no real E66 sample files present")
def test_real_e66_transforms_to_datapoints():
    """A real file must produce one VM datapoint per observation with expected labels."""
    f = _E66_SAMPLES[0]
    r = parse_sdat_xml(f, meter_mappings=SAMPLE_MAPPINGS,
                       physical_production_meters=SAMPLE_PHYSICAL_METERS)
    dps = transform_to_datapoints(r)
    assert len(dps) == len(r.observations)
    m = dps[0]["metric"]
    assert m["project"] == "cel"
    assert m["__name__"].startswith("cel_energy_")
    assert m["data_type"] in ("consumption", "production")
