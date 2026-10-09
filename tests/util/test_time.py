from datetime import UTC, datetime, timedelta, timezone

import pytest

from team_wiki_engine.util.time import KST, kst_iso, kst_parts, parse_instant, to_kst


def test_utc_midnight_is_kst_0900() -> None:
    parts = kst_parts(datetime(2026, 10, 7, 0, 0, tzinfo=UTC))
    assert parts.date == "2026-10-07"
    assert parts.time == "09:00"
    assert parts.run_id == "20261007-0900"


def test_utc_1530_is_next_day_kst_0030() -> None:
    parts = kst_parts(datetime(2026, 10, 7, 15, 30, tzinfo=UTC))
    assert parts.date == "2026-10-08"
    assert parts.time == "00:30"
    assert parts.run_id == "20261008-0030"


def test_kst_midnight_is_0000_not_2400() -> None:
    parts = kst_parts(datetime(2026, 10, 7, 15, 0, tzinfo=UTC))
    assert parts.time == "00:00"
    assert parts.date == "2026-10-08"


def test_naive_datetime_is_treated_as_utc() -> None:
    parts = kst_parts(datetime(2026, 10, 7, 0, 0))
    assert parts.time == "09:00"
    assert parts.date == "2026-10-07"


def test_offset_input_gives_same_result_for_same_instant() -> None:
    utc_input = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
    kst_input = datetime(2026, 10, 7, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    assert kst_parts(kst_input) == kst_parts(utc_input)


# ---------------------------------------------------------------- KST ISO · 읽기


def test_kst_iso_formats_utc_midnight_as_kst_0900() -> None:
    assert kst_iso(datetime(2026, 10, 7, 0, 0, tzinfo=UTC)) == "2026-10-07T09:00:00+09:00"


def test_kst_iso_drops_microseconds() -> None:
    d = datetime(2026, 10, 7, 0, 0, 5, 987654, tzinfo=UTC)
    assert kst_iso(d) == "2026-10-07T09:00:05+09:00"


def test_kst_iso_treats_naive_input_as_utc() -> None:
    assert kst_iso(datetime(2026, 10, 7, 15, 30)) == "2026-10-08T00:30:00+09:00"


def test_to_kst_returns_kst_tzinfo() -> None:
    assert to_kst(datetime(2026, 10, 7, 0, 0, tzinfo=UTC)).tzinfo == KST
    assert to_kst(datetime(2026, 10, 7, 0, 0)).tzinfo == KST


def test_to_kst_keeps_the_same_instant() -> None:
    d = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
    assert to_kst(d) == d


class TestParseInstant:
    @pytest.mark.parametrize(
        "value",
        [
            "2026-10-07T09:00:00+09:00",
            "2026-10-07T00:00:00Z",
            "2026-10-07T00:00:00.000Z",
            "2026-10-07T00:00:00",
        ],
    )
    def test_reads_the_same_instant(self, value: str) -> None:
        # 마지막 값은 naive라 UTC로 읽는다.
        assert parse_instant(value) == datetime(2026, 10, 7, 0, 0, tzinfo=UTC)

    def test_result_is_always_aware(self) -> None:
        parsed = parse_instant("2026-10-07T00:00:00")
        assert parsed is not None
        assert parsed.tzinfo is not None

    @pytest.mark.parametrize(
        "value",
        ["", "9999-99-99", None, 123, "x" * 10_000, "2026-10-07T25:00", b"2026-10-07", ["a"]],
    )
    def test_returns_none_without_raising(self, value: object) -> None:
        assert parse_instant(value) is None

    def test_rejects_valid_values_over_64_chars(self) -> None:
        # fromisoformat은 이 값을 읽는다. 길이 상한이 없으면 None이 아니게 된다.
        long_value = "2026-10-07T00:00:00." + "1" * 50 + "+09:00"
        assert len(long_value) == 76
        assert parse_instant(long_value) is None

    def test_accepts_a_value_of_exactly_64_chars(self) -> None:
        value = "2026-10-07T00:00:00." + "1" * 38 + "+09:00"
        assert len(value) == 64
        assert parse_instant(value) is not None

    @pytest.mark.parametrize("value", ["9999-12-31T23:59:59", "0001-01-01T00:00:00+01:00"])
    def test_returns_none_when_kst_conversion_would_overflow(self, value: str) -> None:
        assert parse_instant(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            "2026-10-07T00:00:00\x00",
            "2026-10-07T00:00:00Z\x00",
            "\x002026-10-07T00:00:00",
            "2026-10-07T00:00:00\x1b",
            "2026-10-07T00:00:00\x7f",
        ],
    )
    def test_rejects_control_characters(self, value: str) -> None:
        assert parse_instant(value) is None

    def test_result_converts_to_kst_without_error(self) -> None:
        parsed = parse_instant("2026-10-07T00:00:00Z")
        assert parsed is not None
        assert kst_iso(parsed) == "2026-10-07T09:00:00+09:00"
