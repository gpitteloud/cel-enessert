"""Shared pytest fixtures and XML builders for parser tests.

Tests construct SDAT XML in-memory rather than depending on the gitignored
input/ sample files, so they run anywhere.
"""
import sys
from pathlib import Path

import pytest

# Make scripts/ importable
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


RSM_OPEN_E66 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rsm:ValidatedMeteredData_16 xmlns:rsm="http://www.strom.ch">'
)
RSM_OPEN_E31 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rsm:AggregatedMeteredData_13 xmlns:rsm="http://www.strom.ch">'
)


def _observations(values, start_seq=1):
    """Render <Observation> elements. values is a list of floats."""
    parts = []
    for i, v in enumerate(values):
        seq = start_seq + i
        parts.append(
            "<rsm:Observation>"
            f"<rsm:Position><rsm:Sequence>{seq}</rsm:Sequence></rsm:Position>"
            f"<rsm:Volume>{v}</rsm:Volume>"
            "<rsm:Condition>21</rsm:Condition>"
            "</rsm:Observation>"
        )
    return "".join(parts)


def make_e66_xml(
    *,
    meter_id="CH101110123450000000000000020576V",
    point="consumption",          # "consumption" | "production" | None (aggregated)
    product_code="2404050010123",
    code_type="VSENationalCode",   # "VSENationalCode" | "ebIXCode"
    values=(1.0, 2.0, 3.0),
    resolution=15,
    resolution_unit="MIN",
    start="2026-05-21T22:00:00Z",
    end="2026-05-26T22:00:00Z",
    community_id="101110-002726",
    include_interval=True,
    include_resolution=True,
    include_metering_data=True,
):
    """Build a ValidatedMeteredData_1.6 (E66) XML document string."""
    if not include_metering_data:
        return RSM_OPEN_E66 + "</rsm:ValidatedMeteredData_16>"

    if point == "consumption":
        mp = (f'<rsm:ConsumptionMeteringPoint>'
              f'<rsm:VSENationalID>{meter_id}</rsm:VSENationalID>'
              f'</rsm:ConsumptionMeteringPoint>')
    elif point == "production":
        mp = (f'<rsm:ProductionMeteringPoint>'
              f'<rsm:VSENationalID>{meter_id}</rsm:VSENationalID>'
              f'</rsm:ProductionMeteringPoint>')
    else:
        mp = ""  # aggregated / no metering point

    interval = ""
    if include_interval:
        interval = (f'<rsm:Interval><rsm:StartDateTime>{start}</rsm:StartDateTime>'
                    f'<rsm:EndDateTime>{end}</rsm:EndDateTime></rsm:Interval>')

    res = ""
    if include_resolution:
        res = (f'<rsm:Resolution><rsm:Resolution>{resolution}</rsm:Resolution>'
               f'<rsm:Unit>{resolution_unit}</rsm:Unit></rsm:Resolution>')

    product = ""
    if product_code:
        product = (f'<rsm:Product><rsm:ID><rsm:{code_type}>{product_code}'
                   f'</rsm:{code_type}></rsm:ID>'
                   f'<rsm:MeasureUnit>KWH</rsm:MeasureUnit></rsm:Product>')

    community = ""
    if community_id:
        community = (f'<rsm:Community><rsm:CommunityID>{community_id}'
                     f'</rsm:CommunityID></rsm:Community>')

    return (
        RSM_OPEN_E66
        + "<rsm:MeteringData>"
        + interval + res + mp + product + community
        + _observations(list(values))
        + "</rsm:MeteringData></rsm:ValidatedMeteredData_16>"
    )


def make_e31_xml(
    *,
    doc_type="E31",
    product_code="2404050010123",
    code_type="VSENationalCode",   # E31 real files use VSENationalCode
    flow="E17",                    # E17 consumption, E18 production
    values=(1.0, 2.0, 3.0),
    resolution=15,
    start="2026-06-10T22:00:00Z",
    end="2026-06-15T22:00:00Z",
    community_id="101110-002726",
    community_type="CT01",
    grid_area="12Y-0000000719-J",
    include_metering_data=True,
    include_start=True,
):
    """Build an AggregatedMeteredData_1.3 (E31) XML document string."""
    header = (
        '<rsm:AggregatedMeteredData_HeaderInformation>'
        '<rsm:BusinessScopeProcess>'
        '<rsm:BusinessReasonType codeListID="VSE">'
        '<rsm:VSENationalCode>C40</rsm:VSENationalCode>'
        '</rsm:BusinessReasonType>'
        '</rsm:BusinessScopeProcess>'
        '<rsm:InstanceDocument>'
        f'<rsm:DocumentType><rsm:ebIXCode>{doc_type}</rsm:ebIXCode></rsm:DocumentType>'
        '</rsm:InstanceDocument>'
        '</rsm:AggregatedMeteredData_HeaderInformation>'
    )

    if not include_metering_data:
        return RSM_OPEN_E31 + header + "</rsm:AggregatedMeteredData_13>"

    interval = "<rsm:Interval>"
    if include_start:
        interval += f"<rsm:StartDateTime>{start}</rsm:StartDateTime>"
    interval += f"<rsm:EndDateTime>{end}</rsm:EndDateTime></rsm:Interval>"

    res = (f'<rsm:Resolution><rsm:Resolution>{resolution}</rsm:Resolution>'
           f'<rsm:Unit>MIN</rsm:Unit></rsm:Resolution>')
    grid = (f'<rsm:MeteringGridArea><rsm:EICID>{grid_area}</rsm:EICID>'
            f'</rsm:MeteringGridArea>')
    product = ""
    if product_code:
        product = (f'<rsm:Product><rsm:ID><rsm:{code_type}>{product_code}'
                   f'</rsm:{code_type}></rsm:ID><rsm:MeasureUnit>KWH</rsm:MeasureUnit>'
                   f'</rsm:Product>')
    agg = ""
    if flow:
        agg = (f'<rsm:AggregationCriteria><rsm:FlowCharacteristic>{flow}'
               f'</rsm:FlowCharacteristic>'
               f'<rsm:SettlementMethodCharacteristic>E02'
               f'</rsm:SettlementMethodCharacteristic></rsm:AggregationCriteria>')
    community = ""
    if community_id:
        community = (f'<rsm:Community><rsm:CommunityID>{community_id}</rsm:CommunityID>'
                     f'<rsm:CommunityType><rsm:VSENationalCode>{community_type}'
                     f'</rsm:VSENationalCode></rsm:CommunityType></rsm:Community>')

    return (
        RSM_OPEN_E31 + header
        + "<rsm:MeteringData>"
        + interval + res + grid + product + agg + community
        + _observations(list(values))
        + "</rsm:MeteringData></rsm:AggregatedMeteredData_13>"
    )


@pytest.fixture
def write_xml(tmp_path):
    """Return a helper that writes XML text to a temp file and returns its Path."""
    def _write(xml_text, name="doc.xml"):
        p = tmp_path / name
        p.write_text(xml_text, encoding="utf-8")
        return p
    return _write
