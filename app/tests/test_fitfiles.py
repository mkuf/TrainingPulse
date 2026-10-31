"""Tests for the generic FIT-file analyzer.

These test the derivation logic three ways:

- ``_build_streams``/``_build_summary`` directly, against plain extracted
  dicts (the shape ``_extract_record``/``_extract_session`` produce) — this is
  where almost all of the actual business logic (unit handling, stream vs.
  session fallback) lives.
- ``analyze_fit_file`` end-to-end, with ``fitdecode.FitReader`` monkeypatched
  to yield small fake frame objects, so the message-dispatch and error-mapping
  behaviour is covered without a real binary fixture.
- ``analyze_fit_file`` on a small activity encoded in the FIT binary format,
  so the real ``fitdecode`` decoding is covered too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import fitdecode
import pytest
from fitfiles import analyzer


def _dt(seconds_from_epoch: int) -> datetime:
    return datetime(2026, 3, 14, 17, 0, 0, tzinfo=timezone.utc) + timedelta(
        seconds=seconds_from_epoch
    )


# ── _build_streams ──────────────────────────────────────────────────


def test_build_streams_extracts_time_relative_to_first_record():
    records = [
        {
            "timestamp": _dt(0),
            "lat": None,
            "lon": None,
            "altitude": 100.0,
            "heart_rate": 120.0,
            "power": 200.0,
            "cadence": 80.0,
            "distance": 0.0,
            "speed": 5.0,
            "temperature": 18.0,
        },
        {
            "timestamp": _dt(5),
            "lat": None,
            "lon": None,
            "altitude": 101.0,
            "heart_rate": 125.0,
            "power": 210.0,
            "cadence": 82.0,
            "distance": 25.0,
            "speed": 5.2,
            "temperature": 18.0,
        },
    ]

    streams = analyzer._build_streams(records)

    assert streams[analyzer.STREAM_TIME] == [0, 5]
    assert streams[analyzer.STREAM_HEARTRATE] == [120.0, 125.0]
    assert streams[analyzer.STREAM_WATTS] == [200.0, 210.0]
    assert streams[analyzer.STREAM_CADENCE] == [80.0, 82.0]
    for key in (analyzer.STREAM_HEARTRATE, analyzer.STREAM_WATTS, analyzer.STREAM_CADENCE):
        assert all(type(v) is int for v in streams[key])
    assert streams[analyzer.STREAM_DISTANCE] == [0.0, 25.0]
    assert streams[analyzer.STREAM_ALTITUDE] == [100.0, 101.0]
    assert streams[analyzer.STREAM_VELOCITY] == [5.0, 5.2]
    assert streams[analyzer.STREAM_TEMP] == [18.0, 18.0]
    assert analyzer.STREAM_LATLNG not in streams


def test_build_streams_sorts_out_of_order_records():
    records = [
        {"timestamp": _dt(10), "lat": None, "lon": None, "altitude": None,
         "heart_rate": 130.0, "power": None, "cadence": None, "distance": None,
         "speed": None, "temperature": None},
        {"timestamp": _dt(0), "lat": None, "lon": None, "altitude": None,
         "heart_rate": 120.0, "power": None, "cadence": None, "distance": None,
         "speed": None, "temperature": None},
    ]

    streams = analyzer._build_streams(records)

    assert streams[analyzer.STREAM_TIME] == [0, 10]
    assert streams[analyzer.STREAM_HEARTRATE] == [120.0, 130.0]


def test_build_streams_includes_latlng_only_when_both_present():
    records = [
        {"timestamp": _dt(0), "lat": 45.0, "lon": 7.0, "altitude": None,
         "heart_rate": None, "power": None, "cadence": None, "distance": None,
         "speed": None, "temperature": None},
        {"timestamp": _dt(1), "lat": None, "lon": None, "altitude": None,
         "heart_rate": None, "power": None, "cadence": None, "distance": None,
         "speed": None, "temperature": None},
    ]

    streams = analyzer._build_streams(records)

    assert streams[analyzer.STREAM_LATLNG] == [[45.0, 7.0], None]


def test_build_streams_drops_a_stream_entirely_when_never_present():
    records = [
        {"timestamp": _dt(0), "lat": None, "lon": None, "altitude": None,
         "heart_rate": None, "power": None, "cadence": None, "distance": None,
         "speed": None, "temperature": None},
    ]

    streams = analyzer._build_streams(records)

    assert set(streams) == {analyzer.STREAM_TIME}


def test_build_streams_of_no_timed_records_is_empty():
    assert analyzer._build_streams([]) == {}
    assert analyzer._build_streams([{"timestamp": None}]) == {}


# ── _build_summary ──────────────────────────────────────────────────


def test_build_summary_prefers_stream_derived_aggregates():
    records = [
        {"timestamp": _dt(0), "lat": None, "lon": None, "altitude": None,
         "heart_rate": 120.0, "power": 200.0, "cadence": None,
         "distance": 0.0, "speed": None, "temperature": None},
        {"timestamp": _dt(10), "lat": None, "lon": None, "altitude": None,
         "heart_rate": 140.0, "power": 220.0, "cadence": None,
         "distance": 50.0, "speed": None, "temperature": None},
    ]
    streams = analyzer._build_streams(records)
    sessions = [{
        "start_time": _dt(-5),
        "total_elapsed_time": 999,
        "total_timer_time": 999,
        "total_distance": 999.0,
        "total_calories": 300.0,
        "total_ascent": 42.0,
        "avg_heart_rate": 999.0,
        "max_heart_rate": 999.0,
        "avg_power": 999.0,
        "max_power": 999.0,
        "avg_speed": 999.0,
    }]

    summary = analyzer._build_summary(records, sessions, streams, "Karoo 3")

    # Stream-derived values win over session totals when a stream exists.
    assert summary.start_date == _dt(0)
    assert summary.elapsed_time == 10
    assert summary.distance == pytest.approx(50.0)
    assert summary.average_heartrate == pytest.approx(130.0)
    assert summary.max_heartrate == pytest.approx(140.0)
    assert summary.average_watts == pytest.approx(210.0)
    assert summary.max_watts == pytest.approx(220.0)
    assert summary.has_heartrate is True
    assert summary.device_watts is True
    assert summary.device_name == "Karoo 3"
    # No fields the streams could produce: falls back to the session.
    assert summary.moving_time == 999
    assert summary.total_elevation_gain == pytest.approx(42.0)
    assert summary.calories == pytest.approx(300.0)


def test_build_summary_falls_back_to_session_without_streams():
    sessions = [{
        "start_time": _dt(0),
        "total_elapsed_time": 3600,
        "total_timer_time": 3500,
        "total_distance": 30000.0,
        "total_calories": 900.0,
        "total_ascent": 500.0,
        "avg_heart_rate": 140.0,
        "max_heart_rate": 170.0,
        "avg_power": 210.0,
        "max_power": 500.0,
        "avg_speed": 8.3,
    }]

    summary = analyzer._build_summary([], sessions, {}, None)

    assert summary.start_date == _dt(0)
    assert summary.elapsed_time == 3600
    assert summary.moving_time == 3500
    assert summary.distance == pytest.approx(30000.0)
    assert summary.average_heartrate == pytest.approx(140.0)
    assert summary.max_heartrate == pytest.approx(170.0)
    assert summary.average_watts == pytest.approx(210.0)
    assert summary.max_watts == pytest.approx(500.0)
    assert summary.average_speed == pytest.approx(8.3)
    assert summary.total_elevation_gain == pytest.approx(500.0)
    assert summary.calories == pytest.approx(900.0)
    assert summary.device_name is None


def test_build_summary_of_nothing_at_all_is_all_zero_or_none():
    summary = analyzer._build_summary([], [], {}, None)

    assert summary.start_date is None
    assert summary.elapsed_time == 0
    assert summary.moving_time == 0
    assert summary.distance is None
    assert summary.average_heartrate is None
    assert summary.has_heartrate is False
    assert summary.device_watts is False


# ── analyze_fit_file (end to end, with a faked FitReader) ───────────


class _FakeFrame:
    def __init__(self, name: str, values: dict, frame_type=None):
        self.name = name
        self._values = values
        self.frame_type = (
            frame_type if frame_type is not None else fitdecode.FIT_FRAME_DATA
        )

    def get_value(self, field_name, fallback=None):
        return self._values.get(field_name, fallback)


class _FakeReader:
    def __init__(self, frames):
        self._frames = frames

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def __iter__(self):
        return iter(self._frames)


def test_analyze_fit_file_dispatches_record_session_and_device_info(monkeypatch):
    frames = [
        _FakeFrame(
            "device_info",
            {"manufacturer": "hammerhead", "product_name": "karoo_3"},
        ),
        _FakeFrame(
            "record",
            {
                "timestamp": _dt(0),
                "heart_rate": 120,
                "power": 200,
                "position_lat": 0,
                "position_long": 0,
            },
        ),
        _FakeFrame(
            "record",
            {"timestamp": _dt(5), "heart_rate": 130, "power": 210},
        ),
        _FakeFrame(
            "session",
            {
                "start_time": _dt(0),
                "total_elapsed_time": 5,
                "total_timer_time": 5,
                "total_ascent": 12.0,
            },
        ),
        # Non-data frames (header/crc-like) must be ignored.
        _FakeFrame("crc", {}, frame_type=fitdecode.FIT_FRAME_CRC),
    ]
    monkeypatch.setattr(
        analyzer.fitdecode, "FitReader", lambda *_a, **_kw: _FakeReader(frames)
    )

    analysis = analyzer.analyze_fit_file(b"irrelevant-bytes")

    assert analysis.streams[analyzer.STREAM_TIME] == [0, 5]
    assert analysis.streams[analyzer.STREAM_HEARTRATE] == [120, 130]
    assert analysis.summary.average_heartrate == pytest.approx(125.0)
    assert analysis.summary.total_elevation_gain == pytest.approx(12.0)
    assert analysis.summary.device_name == "Hammerhead Karoo 3"


def test_analyze_fit_file_wraps_fitdecode_errors(monkeypatch):
    class _RaisingReader:
        def __enter__(self):
            raise fitdecode.FitHeaderError("not a FIT file")

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        analyzer.fitdecode, "FitReader", lambda *_a, **_kw: _RaisingReader()
    )

    with pytest.raises(analyzer.FitParseError):
        analyzer.analyze_fit_file(b"not-a-fit-file")


def test_analyze_fit_file_skips_one_malformed_message_and_keeps_going(monkeypatch):
    class _ExplodingFrame(_FakeFrame):
        def get_value(self, field_name, fallback=None):
            raise RuntimeError("boom")

    frames = [
        _ExplodingFrame("record", {}),
        _FakeFrame("record", {"timestamp": _dt(0), "heart_rate": 99}),
    ]
    monkeypatch.setattr(
        analyzer.fitdecode, "FitReader", lambda *_a, **_kw: _FakeReader(frames)
    )

    analysis = analyzer.analyze_fit_file(b"irrelevant-bytes")

    assert analysis.streams[analyzer.STREAM_TIME] == [0]
    assert analysis.streams[analyzer.STREAM_HEARTRATE] == [99]


# ── analyze_fit_file on real bytes ──────────────────────────────────
#
# The tests above fake ``fitdecode``. This one encodes a small activity in the
# FIT binary format, so a ``fitdecode`` upgrade that changes field names,
# scaling, or decoded types fails here.

_FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)  # fmt: skip
#: FIT base type id -> struct format.
_BASE_TYPES = {0x00: "B", 0x01: "b", 0x02: "B", 0x84: "H", 0x85: "i", 0x86: "I"}


def _fit_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        for nibble in (byte & 0xF, byte >> 4):
            tmp = _CRC_TABLE[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc ^= tmp ^ _CRC_TABLE[nibble]
    return crc


def _fit_time(value: datetime) -> int:
    return int((value - _FIT_EPOCH).total_seconds())


def _semicircles(degrees: float) -> int:
    return round(degrees * 2**31 / 180)


def _encode_fit(messages: list[tuple[int, list[tuple[int, int, object]]]]) -> bytes:
    """``messages`` is ``[(global number, [(field, base type, raw value)])]``.

    Strings are passed as ``bytes`` with base type 0x07 and a fixed size.
    """
    import struct

    body = b""
    for local, (global_number, fields) in enumerate(messages):
        local %= 16
        definition = struct.pack("<BBBHB", 0x40 | local, 0, 0, global_number, len(fields))
        values = bytes([local])
        for number, base_type, value in fields:
            if base_type == 0x07:
                encoded = value
            else:
                encoded = struct.pack("<" + _BASE_TYPES[base_type], value)
            definition += bytes([number, len(encoded), base_type])
            values += encoded
        body += definition + values
    header = struct.pack("<BBHI4s", 14, 0x10, 2100, len(body), b".FIT")
    header += struct.pack("<H", _fit_crc(header))
    data = header + body
    return data + struct.pack("<H", _fit_crc(data))


def test_analyze_fit_file_decodes_real_fit_bytes():
    start = _dt(0)
    file_id = (0, [(0, 0x00, 4), (1, 0x84, 1), (4, 0x86, _fit_time(start))])
    device = (23, [(253, 0x86, _fit_time(start)), (2, 0x84, 1), (27, 0x07, b"Edge 530\0")])
    records = [
        (
            20,
            [
                (253, 0x86, _fit_time(_dt(i))),
                (0, 0x85, _semicircles(47.0 + i / 1000)),
                (1, 0x85, _semicircles(11.0)),
                (2, 0x84, (100 + i + 500) * 5),  # altitude: scale 5, offset 500
                (3, 0x02, 120 + i),
                (4, 0x02, 85),
                (5, 0x86, 800 * i),  # distance: scale 100
                (6, 0x84, 8000),  # speed: scale 1000
                (7, 0x84, 200 + 10 * i),
                (13, 0x01, 18),
            ],
        )
        for i in range(10)
    ]
    session = (
        18,
        [
            (253, 0x86, _fit_time(_dt(9))),
            (2, 0x86, _fit_time(start)),
            (5, 0x00, 2),  # cycling
            (7, 0x86, 9000),  # total_elapsed_time: scale 1000
            (8, 0x86, 8000),  # total_timer_time
            (9, 0x86, 7200),  # total_distance: scale 100
            (11, 0x84, 12),
            (14, 0x84, 8000),
            (15, 0x84, 8500),
            (16, 0x02, 124),
            (17, 0x02, 129),
            (18, 0x02, 85),
            (19, 0x02, 90),
            (20, 0x84, 245),
            (21, 0x84, 290),
            (22, 0x84, 9),
            (23, 0x84, 0),
        ],
    )

    analysis = analyzer.analyze_fit_file(_encode_fit([file_id, device, *records, session]))

    streams = analysis.streams
    assert streams[analyzer.STREAM_TIME] == list(range(10))
    assert streams[analyzer.STREAM_HEARTRATE] == list(range(120, 130))
    assert streams[analyzer.STREAM_WATTS] == list(range(200, 300, 10))
    assert streams[analyzer.STREAM_CADENCE] == [85] * 10
    assert streams[analyzer.STREAM_DISTANCE] == pytest.approx([8.0 * i for i in range(10)])
    assert streams[analyzer.STREAM_ALTITUDE] == pytest.approx([100.0 + i for i in range(10)])
    assert streams[analyzer.STREAM_VELOCITY] == pytest.approx([8.0] * 10)
    assert streams[analyzer.STREAM_TEMP] == [18] * 10
    lat, lon = streams[analyzer.STREAM_LATLNG][3]
    assert (lat, lon) == (pytest.approx(47.003, abs=1e-6), pytest.approx(11.0, abs=1e-6))

    summary = analysis.summary
    assert summary.start_date == start
    assert (summary.elapsed_time, summary.moving_time) == (9, 8)
    assert summary.distance == pytest.approx(72.0)
    assert summary.average_heartrate == pytest.approx(124.5)
    assert summary.max_heartrate == 129
    assert summary.average_watts == pytest.approx(245.0)
    assert summary.max_watts == 290
    assert summary.average_speed == pytest.approx(8.0)
    assert summary.max_speed == pytest.approx(8.5)
    assert (summary.average_cadence, summary.max_cadence) == (85, 90)
    assert (summary.total_elevation_gain, summary.total_descent) == (9, 0)
    assert summary.calories == 12
    assert summary.device_name == "Garmin Edge 530"
