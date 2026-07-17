"""Tests for parse_sdat_e31_aggregated (AggregatedMeteredData_1.3)."""
import pytest

from parse_sdat_e31_aggregated import (
    parse_e31_xml,
    transform_e31_to_datapoints,
)
from conftest import make_e31_xml


# --------------------------------------------------------------------------
# parse_e31_xml - metadata extraction
# --------------------------------------------------------------------------

def test_parse_basic_metadata(write_xml):
    f = write_xml(make_e31_xml(flow="E17", product_code="2404050010123"))
    r = parse_e31_xml(f)
    assert r.document_type == "E31"
    assert r.community_id == "101110-002726"
    assert r.community_type == "CT01"
    assert r.product_code == "2404050010123"
    assert r.product_code_type == "VSE"
    assert r.flow_characteristic == "E17"
    assert r.grid_area == "12Y-0000000719-J"
    assert r.business_reason == "C40"
    assert r.resolution_minutes == 15


def test_flow_e18_production(write_xml):
    f = write_xml(make_e31_xml(flow="E18"))
    r = parse_e31_xml(f)
    assert r.flow_characteristic == "E18"


def test_ebix_product_code(write_xml):
    f = write_xml(make_e31_xml(product_code="8716867000030", code_type="ebIXCode"))
    r = parse_e31_xml(f)
    assert r.product_code == "8716867000030"
    assert r.product_code_type == "ebIX"


# --------------------------------------------------------------------------
# Observations & timestamps
# --------------------------------------------------------------------------

def test_observations_with_timestamps(write_xml):
    f = write_xml(make_e31_xml(values=(10.0, 20.0, 30.0),
                               start="2026-06-10T22:00:00Z",
                               resolution=15))
    r = parse_e31_xml(f)
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
    r = parse_e31_xml(f)
    assert len(r.observations) == 96


# --------------------------------------------------------------------------
# Rejection / edge cases
# --------------------------------------------------------------------------

def test_non_e31_document_returns_none(write_xml):
    # An E66 doc type must be rejected by the E31 parser
    f = write_xml(make_e31_xml(doc_type="E66"))
    assert parse_e31_xml(f) is None


def test_no_metering_data_returns_none(write_xml):
    f = write_xml(make_e31_xml(include_metering_data=False))
    assert parse_e31_xml(f) is None


def test_missing_start_datetime_returns_no_observations(write_xml):
    # Without StartDateTime the parser cannot compute timestamps
    f = write_xml(make_e31_xml(include_start=False))
    r = parse_e31_xml(f)
    assert r.observations == []


def test_malformed_xml_returns_none(write_xml):
    f = write_xml("<rsm:AggregatedMeteredData_13><broken>", name="bad.xml")
    # parser catches exceptions and returns None
    assert parse_e31_xml(f) is None


# --------------------------------------------------------------------------
# transform_e31_to_datapoints
# --------------------------------------------------------------------------

def test_transform_builds_vm_datapoints(write_xml):
    f = write_xml(make_e31_xml(flow="E17", product_code="2404050010123",
                               values=(5.0, 6.0)))
    r = parse_e31_xml(f)
    dps = transform_e31_to_datapoints(r)
    assert len(dps) == 2
    m = dps[0]["metric"]
    assert m["__name__"] == "energy_community_aggregate_kwh"
    assert m["project"] == "cel"
    assert m["community_id"] == "101110-002726"
    assert m["product_code"] == "2404050010123"
    assert m["flow_characteristic"] == "E17"
    assert m["data_source"] == "E31_AggregatedMeteredData"
    assert dps[0]["values"] == [5.0]
    assert isinstance(dps[0]["timestamps"][0], int)


def test_transform_includes_condition_label(write_xml):
    f = write_xml(make_e31_xml(values=(1.0,)))
    r = parse_e31_xml(f)
    dps = transform_e31_to_datapoints(r)
    assert dps[0]["metric"]["condition"] == "21"


def test_transform_empty_input():
    assert transform_e31_to_datapoints(None) == []


def test_transform_project_label_present(write_xml):
    # Regression: E31 data must carry project=cel to match E66 label scheme
    f = write_xml(make_e31_xml())
    r = parse_e31_xml(f)
    dps = transform_e31_to_datapoints(r)
    assert all(dp["metric"]["project"] == "cel" for dp in dps)
