"""KST 기준 날짜·시각·run_id."""

from datetime import UTC, datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


class KstParts(NamedTuple):
    date: str  # YYYY-MM-DD
    time: str  # HH:MM
    run_id: str  # YYYYMMDD-HHMM


_MAX_INSTANT_LENGTH = 64


def to_kst(d: datetime) -> datetime:
    """`d`를 KST 시각으로 바꾼다(같은 순간). naive 값은 UTC로 간주한다."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(KST)


def kst_iso(d: datetime) -> str:
    """KST ISO 문자열(초 단위, 마이크로초는 버린다). 예: `2026-10-07T09:00:00+09:00`."""
    return to_kst(d).isoformat(timespec="seconds")


def parse_instant(value: object) -> datetime | None:
    """ISO 8601 문자열을 aware `datetime`으로 읽는다(`Z` 접미 허용, naive는 UTC).

    문자열이 아니거나 64자를 넘거나 제어 문자가 있거나 읽을 수 없거나 KST·UTC로 바꿀 수 없으면
    `None`이다. 예외를 던지지 않는다.
    """
    if not isinstance(value, str) or len(value) > _MAX_INSTANT_LENGTH or not value.isprintable():
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        # 표현 범위를 벗어나는 값(예: 9999-12-31 → KST)은 뒤에서 터지지 않게 여기서 거른다.
        to_kst(parsed)
        parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None
    return parsed


def kst_parts(d: datetime) -> KstParts:
    """`d`를 KST로 바꿔 날짜·시각·run_id를 돌려준다. naive 값은 UTC로 간주한다."""
    local = to_kst(d)
    return KstParts(
        date=local.strftime("%Y-%m-%d"),
        time=local.strftime("%H:%M"),
        run_id=local.strftime("%Y%m%d-%H%M"),
    )
