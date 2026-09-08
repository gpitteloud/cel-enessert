"""Tests for sdat_header - what one SDAT file is, read in a single pass.

Every other stage now works off a FileHeader: routing (E66 vs E31, CEL vs RCP),
attribution, the delivery report and provenance. So the
paths are asserted here rather than through each consumer, and the golden test
at the end checks them against a real delivery -- the header element is named
after the document type, which is exactly the trap the root-anchored paths avoid.
"""
import xml.etree.ElementTree as ET

import pytest

from conftest import SAMPLE_DIR, make_e31_xml, make_e66_xml, meter_id
from scripts.models import MetricType
from scripts.sdat_header import (FileHeader, load_headers, parse_header,
                                read_header, ts_from_filename)

REAL_NAME = ('20260610_094525_12X-0000001536-1_E66_12X-00000020FW-5_'
             '57a91dfa-64a0-11f1-be90-00000084413a.xml')


def header(xml, file_name=REAL_NAME):
    return parse_header(ET.fromstring(xml), file_name)


# --------------------------------------------------------------------------
# The provider's clock comes from the filename
# --------------------------------------------------------------------------

def test_ts_and_delivery_come_from_the_filename():
    """Derived without reading the file, which is what makes re-ingesting the
    same file write the same header row (DEDUP UPSERT KEYS(ts, file_name))."""
    ts, delivery = ts_from_filename(REAL_NAME)
    assert (ts.year, ts.month, ts.day) == (2026, 6, 10)
    assert (ts.hour, ts.minute, ts.second) == (9, 45, 25)
    assert delivery == '20260610'


def test_a_name_without_the_prefix_has_no_timestamp():
    """A file the provider did not name is still parseable; it just has no
    provenance timestamp, so no header row can be written for it."""
    assert ts_from_filename('doc.xml') == (None, None)
    assert header(make_e66_xml(), 'doc.xml').ts is None


# --------------------------------------------------------------------------
# E66
# --------------------------------------------------------------------------

def test_e66_header_fields():
    h = header(make_e66_xml(meter_id=meter_id('0020576V')))
    assert h.document_type == 'E66'
    assert h.file_meter_id == meter_id('0020576V')
    assert h.metering_point_type == 'consumption'
    assert h.flow_characteristic is None
    assert h.community_id == '101110-002726'
    assert h.product_code == '2404050010123'
    assert h.code_type == 'VSENationalCode'
    assert h.metric_type is MetricType.CONSUMPTION_LOCAL
    assert (h.direction, h.segment) == ('consumption', 'cel')


def test_the_report_period_is_the_grouping_key():
    h = header(make_e66_xml(start='2026-05-21T22:00:00Z',
                            end='2026-05-26T22:00:00Z'))
    start, end = h.report_period
    assert (start.day, end.day) == (21, 26)


def test_observations_come_from_the_same_parse():
    """The values are on the header so a batch reads each file once: the delivery
    report compares them slot by slot without reparsing."""
    h = header(make_e66_xml(values=(1.5, 2.25), start='2026-05-21T22:00:00Z'))
    assert h.observation_count == 2
    assert [str(v) for v in h.values] == ['1.5', '2.25']
    assert h.observations[0].timestamp.startswith('2026-05-21T22:00:00')
    assert '22:15:00' in h.observations[1].timestamp


def test_resolution_drives_the_observation_clock():
    h = header(make_e66_xml(resolution=30, start='2026-05-21T22:00:00Z'))
    assert h.resolution_minutes == 30
    assert '22:30:00' in h.observations[1].timestamp


def test_attributed_meter_defaults_to_the_files_own_meter():
    h = header(make_e66_xml(meter_id=meter_id('0020576V')))
    assert h.attributed_meter_id == h.file_meter_id


# --------------------------------------------------------------------------
# Domain routing
# --------------------------------------------------------------------------

def test_c40_is_cel_and_e88_is_rcp():
    """The child element name is the discriminator: the codeListID attribute
    says "VSE" on an RCP file too."""
    assert header(make_e66_xml()).rcp is False
    rcp = header(make_e66_xml(business_reason='E88',
                              reason_code_type='ebIXCode', community_id=None))
    assert rcp.rcp is True
    assert rcp.business_reason == 'E88'
    assert rcp.reason_code_type == 'ebIXCode'
    assert rcp.community_id is None


def test_an_unknown_business_reason_is_refused():
    """A third domain must fail loudly rather than be counted as community
    energy."""
    with pytest.raises(ValueError, match='C40'):
        header(make_e66_xml(business_reason='E99'))
    with pytest.raises(ValueError):
        header(make_e66_xml(business_reason=None))


# --------------------------------------------------------------------------
# E31
# --------------------------------------------------------------------------

def test_e31_direction_comes_from_the_flow_characteristic():
    """E31 has no metering point: the aggregate belongs to the community."""
    h = header(make_e31_xml(flow='E18', product_code='8716867000030',
                            code_type='ebIXCode'))
    assert h.document_type == 'E31'
    assert h.file_meter_id is None and h.metering_point_type is None
    assert h.flow_characteristic == 'E18'
    assert h.direction == 'production' and h.segment == 'total'
    assert h.community_type == 'CT01'
    assert h.grid_area == '12Y-0000000719-J'


# --------------------------------------------------------------------------
# Unusable documents
# --------------------------------------------------------------------------

@pytest.mark.parametrize('xml, missing', [
    (make_e66_xml(include_metering_data=False), 'MeteringData'),
    (make_e66_xml(include_resolution=False), 'Resolution'),
    (make_e66_xml(include_interval=False), 'Interval'),
    (make_e31_xml(include_start=False), 'Interval'),
])
def test_a_document_that_cannot_be_timestamped_is_refused(xml, missing):
    with pytest.raises(ValueError, match=missing):
        header(xml)


def test_load_headers_skips_what_it_cannot_read(tmp_path):
    """One bad file must not abort the delivery; the caller sees a missing name
    and fails that file alone."""
    good = tmp_path / REAL_NAME
    good.write_text(make_e66_xml(), encoding='utf-8')
    bad = tmp_path / '20260610_094525_broken.xml'
    bad.write_text('not xml at all', encoding='utf-8')

    headers = load_headers([good, bad, tmp_path / 'gone.xml'])
    assert list(headers) == [good.name]
    assert isinstance(headers[good.name], FileHeader)


# --------------------------------------------------------------------------
# Golden test on a real file
# --------------------------------------------------------------------------

def _real_e66():
    files = sorted(f for f in SAMPLE_DIR.glob('20260610_*.xml')
                   if f.stat().st_size) if SAMPLE_DIR.is_dir() else []
    if not files:
        pytest.skip('real sample files not present')
    return files


def test_a_real_file_fills_every_provenance_field():
    h = read_header(_real_e66()[0])
    assert h.document_type == 'E66'
    assert h.document_id and h.creation is not None
    assert (h.sender_role, h.receiver_role) == ('MDR', 'CEM')
    assert (h.business_reason, h.reason_code_type) == ('C40', 'VSENationalCode')
    assert h.resolution_minutes == 15
    assert h.observation_count % 96 == 0
    # ReportPeriod == Interval in every real file, so the header's period is the
    # observation period.
    assert h.period_start.isoformat() == h.observations[0].timestamp
    assert h.file_meter_id and len(h.file_meter_id) == 33


def test_every_real_file_of_a_delivery_reads():
    files = _real_e66()
    headers = load_headers(files)
    assert len(headers) == len(files)
    assert {h.document_type for h in headers.values()} <= {'E66', 'E31'}
    assert all(h.ts and h.delivery == '20260610' for h in headers.values())
