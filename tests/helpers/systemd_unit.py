"""systemd 유닛 문법 검사기(systemd.syntax(7)의 부분집합).

맥에는 `systemd-analyze`가 없어서 순수 Python으로 검사한다. systemd는 모르는 키를
경고만 하고 무시하므로(`ProtectSytem=` 같은 오타가 보안 설정을 조용히 끈다),
`KNOWN_KEYS`에 없는 키를 테스트가 잡는다.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_SECTION = re.compile(r"^\[([A-Za-z]+)\]$")
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class UnitSyntaxError(ValueError):
    """유닛 글의 문법 오류(줄 번호와 이유)."""


@dataclass(frozen=True)
class UnitFile:
    name: str
    sections: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)

    def values(self, section: str, key: str) -> tuple[str, ...]:
        return self.sections.get(section, {}).get(key, ())

    def one(self, section: str, key: str) -> str:
        found = self.values(section, key)
        assert len(found) == 1, f"{self.name} [{section}] {key}: 정확히 하나여야 함 {found!r}"
        return found[0]

    def has(self, section: str, key: str) -> bool:
        return key in self.sections.get(section, {})

    def env(self) -> dict[str, str]:
        pairs: dict[str, str] = {}
        for item in self.values("Service", "Environment"):
            name, sep, value = item.partition("=")
            assert sep, f"{self.name}: Environment 항목에 '='가 없음 {item!r}"
            pairs[name] = value
        return pairs


KNOWN_KEYS: Mapping[str, frozenset[str]] = {
    "Unit": frozenset(
        {"Description", "Wants", "After", "OnFailure", "StartLimitIntervalSec", "StartLimitBurst"}
    ),
    "Service": frozenset(
        {
            "Type", "NotifyAccess", "WatchdogSec", "User", "Group", "WorkingDirectory",
            "Environment", "EnvironmentFile", "ExecStartPre", "ExecStart", "Restart",
            "RestartSec", "RestartPreventExitStatus", "SuccessExitStatus", "TimeoutStartSec",
            "TimeoutStopSec", "KillMode", "KillSignal", "MemoryMax", "TasksMax", "UMask",
            "NoNewPrivileges", "PrivateTmp", "PrivateDevices", "ProtectSystem", "ProtectHome",
            "ReadWritePaths", "ProtectKernelTunables", "ProtectKernelModules",
            "ProtectKernelLogs", "ProtectControlGroups", "ProtectClock", "ProtectHostname",
            "RestrictNamespaces", "RestrictRealtime", "RestrictSUIDSGID", "LockPersonality",
            "SystemCallArchitectures", "CapabilityBoundingSet", "AmbientCapabilities",
            "RestrictAddressFamilies",
        }
    ),  # fmt: skip
    "Timer": frozenset({"OnCalendar", "Persistent", "RandomizedDelaySec", "AccuracySec", "Unit"}),
    "Install": frozenset({"WantedBy"}),
    "Journal": frozenset({"Storage", "SystemMaxUse", "SystemKeepFree", "MaxRetentionSec"}),
}


def parse_unit(text: str, name: str = "<unit>") -> UnitFile:
    if "\r" in text:
        line_no = text[: text.index("\r")].count("\n") + 1
        raise UnitSyntaxError(f"{name}:{line_no}: CR 문자는 허용하지 않습니다(LF만)")
    # 줄 이음: 줄 끝 `\`는 다음 줄과 공백 하나로 잇는다(줄 번호는 첫 줄 기준).
    logical: list[tuple[int, str]] = []
    pending: str | None = None
    start = 0
    for number, raw in enumerate(text.split("\n"), start=1):
        if pending is None:
            start = number
            current = raw
        else:
            current = pending + " " + raw.strip()
        if current.endswith("\\"):
            pending = current[:-1].rstrip()
            continue
        pending = None
        logical.append((start, current))
    if pending is not None:
        logical.append((start, pending))

    sections: dict[str, dict[str, list[str]]] = {}
    current_section: str | None = None
    for number, raw in logical:
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        header = _SECTION.match(line)
        if header:
            current_section = header.group(1)
            sections.setdefault(current_section, {})
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            raise UnitSyntaxError(f"{name}:{number}: 알 수 없는 줄 {line!r}")
        if current_section is None:
            raise UnitSyntaxError(f"{name}:{number}: 절 앞의 할당 {line!r}")
        sections[current_section].setdefault(key, []).append(value.strip())
    return UnitFile(
        name=name,
        sections={s: {k: tuple(v) for k, v in keys.items()} for s, keys in sections.items()},
    )


def load_unit(path: Path) -> UnitFile:
    return parse_unit(path.read_text(encoding="utf-8"), path.name)


_UNITS: dict[str, float] = {}
for _names, _secs in (
    (("us", "usec"), 1e-6),
    (("ms", "msec"), 1e-3),
    (("s", "sec", "second", "seconds"), 1.0),
    (("m", "min", "minute", "minutes"), 60.0),
    (("h", "hr", "hour", "hours"), 3600.0),
    (("d", "day", "days"), 86400.0),
    (("w", "week", "weeks"), 604800.0),
    (("M", "month", "months"), 2629800.0),  # 30.44일
    (("y", "year", "years"), 31557600.0),  # 365.25일
):
    for _n in _names:
        _UNITS[_n] = _secs
_TOKEN = re.compile(r"^(\d+)([A-Za-z]*)$")


def parse_timespan(text: str) -> float:
    stripped = text.strip()
    if stripped == "infinity":
        return math.inf
    tokens = stripped.split()
    if not tokens:
        raise ValueError(f"빈 시간 값: {text!r}")
    total = 0.0
    for token in tokens:
        match = _TOKEN.match(token)
        if not match:
            raise ValueError(f"읽을 수 없는 시간 토큰 {token!r} in {text!r}")
        unit = match.group(2) or "s"
        if unit not in _UNITS:
            raise ValueError(f"알 수 없는 시간 단위 {unit!r} in {text!r}")
        total += int(match.group(1)) * _UNITS[unit]
    return total


_BYTES = re.compile(r"^(\d+)([KMGT]?)$")


def parse_bytes(text: str) -> int:
    match = _BYTES.match(text.strip())
    if not match:
        raise ValueError(f"읽을 수 없는 크기 값: {text!r}")
    power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}[match.group(2)]
    result: int = int(match.group(1)) * (1024**power)
    return result


@dataclass(frozen=True)
class TmpfilesLine:
    type: str
    path: str
    mode: str
    user: str
    group: str
    age: str


def parse_tmpfiles(text: str) -> list[TmpfilesLine]:
    lines: list[TmpfilesLine] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) not in (5, 6):
            raise ValueError(f"tmpfiles 필드 수가 5~6이 아님: {line!r}")
        if len(parts) == 5:
            parts.append("-")
        lines.append(TmpfilesLine(*parts))
    return lines


def parse_env_file(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not _ENV_KEY.match(key):
            raise ValueError(f"KEY=VALUE 줄이 아님: {line!r}")
        pairs.append((key, value))
    return pairs
