"""Tests for the generic FIT-file analyzer.

Hand-authoring valid binary .fit bytes is impractical, so these test the
derivation logic two ways:

- ``_build_streams``/``_build_summary`` directly, against plain extracted
  dicts (the shape ``_extract_record``/``_extract_session`` produce) — this is
  where almost all of the actual business logic (unit handling, stream vs.
  session fallback) lives.
- ``analyze_fit_file`` end-to-end, with ``fitdecode.FitReader`` monkeypatched
  to yield small fake frame objects, so the message-dispatch and error-mapping
  behaviour is covered without a real binary fixture.
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
