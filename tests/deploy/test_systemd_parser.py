"""systemd 문법 파서 자체 시험(검사기가 공허하지 않게)."""

from __future__ import annotations

import math

import pytest

from tests.helpers.systemd_unit import (
    UnitSyntaxError,
    parse_bytes,
    parse_env_file,
    parse_timespan,
    parse_tmpfiles,
    parse_unit,
)

SAMPLE = """\
# 주석
; 다른 주석

[Unit]
Description=설명 = 등호 포함
After=a.target

[Service]
Environment=A=1
Environment=B=2
ExecStart=/bin/true \\
  --flag
Empty=

[Unit]
Wants=b.target
"""


def test_sections_assignments_comments_continuation_and_merge() -> None:
    unit = parse_unit(SAMPLE, "x.service")
    assert unit.name == "x.service"
    assert unit.one("Unit", "Description") == "설명 = 등호 포함"
    assert unit.values("Unit", "After") == ("a.target",)
    assert unit.one("Unit", "Wants") == "b.target"  # 같은 절은 합쳐진다
    assert unit.values("Service", "Environment") == ("A=1", "B=2")
    assert unit.env() == {"A": "1", "B": "2"}
    assert unit.one("Service", "ExecStart") == "/bin/true --flag"
    assert unit.has("Service", "Empty")
    assert unit.one("Service", "Empty") == ""
    assert not unit.has("Service", "Nope")
    assert unit.values("Nope", "Nope") == ()


def test_one_requires_exactly_one() -> None:
    unit = parse_unit(SAMPLE)
    with pytest.raises(AssertionError):
        unit.one("Service", "Environment")
    with pytest.raises(AssertionError):
        unit.one("Service", "Missing")


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("A=1\n[Unit]\n", 1),
        ("[Unit]\nthis is garbage\n", 2),
        ("[Unit]\n[Bad Name]\n", 2),
        ("[Unit]\n1bad=x\n", 2),
        ("[Unit]\r\nA=1\r\n", 1),
        ("[Unit]\nA=1\n=x\n", 3),
    ],
)
def test_syntax_errors_name_the_line(text: str, line: int) -> None:
    with pytest.raises(UnitSyntaxError) as info:
        parse_unit(text)
    assert f"{line}" in str(info.value)


def test_timespan() -> None:
    assert parse_timespan("2h") == 7200
    assert parse_timespan("15min") == 900
    assert parse_timespan("90s") == 90
    assert parse_timespan("1h 30min") == 5400
    assert parse_timespan("11h") == 39600
    assert parse_timespan("infinity") == math.inf
    assert parse_timespan("30") == 30
    assert parse_timespan("5min") == 300
    assert parse_timespan("3month") == pytest.approx(7_889_400, rel=0.001)
    assert parse_timespan("1m") == 60
    assert parse_timespan("1M") != parse_timespan("1m")
    assert parse_timespan("1500ms") == 1.5
    for bad in ("10x", "", "min", "1 h 2"):
        with pytest.raises(ValueError):
            parse_timespan(bad)


def test_bytes() -> None:
    assert parse_bytes("3G") == 3 * 2**30
    assert parse_bytes("512M") == 512 * 2**20
    assert parse_bytes("4K") == 4096
    assert parse_bytes("100") == 100
    for bad in ("10%", "infinity", "", "1.5G", "3g"):
        with pytest.raises(ValueError):
            parse_bytes(bad)


def test_tmpfiles() -> None:
    lines = parse_tmpfiles("# c\n\nd /a 0750 u g -\nd /b 0700 root root 30d\nd /c 0700 u g\n")
    assert [(x.path, x.mode, x.user, x.group, x.age) for x in lines] == [
        ("/a", "0750", "u", "g", "-"),
        ("/b", "0700", "root", "root", "30d"),
        ("/c", "0700", "u", "g", "-"),
    ]
    assert lines[0].type == "d"
    for bad in ("d /a 0750 u", "d /a 0750 u g - x y"):
        with pytest.raises(ValueError):
            parse_tmpfiles(bad)


def test_env_file() -> None:
    assert parse_env_file("# c\n\nA_1=\nB=x y\n") == [("A_1", ""), ("B", "x y")]
    for bad in ("a=1", "NOEQUALS", "1A=2"):
        with pytest.raises(ValueError):
            parse_env_file(bad)
