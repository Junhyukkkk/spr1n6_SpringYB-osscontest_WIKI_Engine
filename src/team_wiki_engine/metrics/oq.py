"""미결 질문 파싱과 추적(`wiki/open-questions.md`의 `### ` 항목).

파일은 믿을 수 없는 입력이다(사람이 고치거나 Claude가 쓴다). 파서는 줄 단위로 한 번만 훑고(정규식
없음 — 길이가 아무리 길어도 선형), 코드 펜스 안의 줄은 무시하며, 닫히지 않은 펜스는 파일 끝까지
이어진 것으로 보되, 파일 끝에서 아직 열려 있으면 그 여는 줄을 일반 글로 보고 다시 읽는다(낱개로 남은
펜스 하나가 뒤의 모든 항목을 가려 해소·신규 사건을 거짓으로 만드는 것을 막는다). 예외를 던지지 않고,
제목은 이스케이프한 뒤 300자로 자르며(다시 다듬어도 같은 값이다), 항목은 1000개까지만 본다.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, NamedTuple

from pydantic import BaseModel, ConfigDict, FailFast, Field, TypeAdapter

from team_wiki_engine.metrics.record import (
    MAX_STATE_JSON,
    STATE_FILES,
    OqEventRecord,
    append_jsonl,
    read_json,
    write_json,
)
from team_wiki_engine.util.fs import read_text_if_exists
from team_wiki_engine.util.time import kst_iso
from team_wiki_engine.wiki.lint_secrets import clean

MAX_OQ_ITEMS = 1000
"""한 번에 보는 미결 항목 수 상한."""
MAX_TITLE_CHARS = 300
"""제목 길이 상한(이스케이프한 뒤 기준)."""

_HEADING: Final = "### "
_FENCE_CHARS: Final = "`~"
_MAX_FENCE_INDENT: Final = 3
_MAX_RESCANS: Final = 4
"""낱개 펜스를 일반 글로 바꿔 다시 읽는 최대 횟수. 넘으면 펜스를 모두 일반 글로 본다(시간 상한)."""
_SNAPSHOT_BUDGET: Final = MAX_STATE_JSON - 4096
"""스냅샷 제목들이 쓸 수 있는 바이트(들여쓴 JSON 기준). 나머지는 괄호·키 몫이다."""


class _Fence(NamedTuple):
    char: str
    length: int
    rest: str


def _fence_of(line: str) -> _Fence | None:
    """줄이 코드 펜스(백틱 또는 물결 3개 이상, 들여쓰기 3칸 이하)이면 그 모양."""
    indent = len(line) - len(line.lstrip(" "))
    if indent > _MAX_FENCE_INDENT or indent >= len(line):
        return None
    char = line[indent]
    if char not in _FENCE_CHARS:
        return None
    body = line[indent:]
    rest = body.lstrip(char)
    length = len(body) - len(rest)
    return _Fence(char, length, rest) if length >= 3 else None


def _title(raw: str) -> str:
    """제목 정규화. 멱등이다: `_title(_title(x)) == _title(x)`(스냅샷과 다시 비교해도 같다).

    이스케이프는 글자마다 1글자 이상으로 늘어나므로 앞 300자만 처리해도 결과의 앞 300자는 같다.
    자른 끝이 공백이면 그 공백은 뗀다(안 떼면 저장한 값을 다시 다듬을 때 달라진다).
    """
    return clean(raw.strip()[:MAX_TITLE_CHARS])[:MAX_TITLE_CHARS].rstrip()


def _is_mismatch_line(line: str) -> bool:
    """`- 종류: 어긋남`으로 시작하는 줄인가(들여쓰기·공백 허용). 선형으로 확인한다."""
    rest = line.lstrip()
    if not rest.startswith("-"):
        return False
    rest = rest[1:].lstrip()
    if not rest.startswith("종류:"):
        return False
    return rest[len("종류:") :].lstrip().startswith("어긋남")


def _parse_once(
    lines: list[str], plain: frozenset[int] | None, limit: int, only_mismatch: bool
) -> tuple[list[str], int | None]:
    """한 번 훑는다. `plain`에 든 줄 번호는 펜스로 보지 않고(`None`이면 모든 줄), 파일 끝에서 열려
    있는 펜스가 있으면 그 여는 줄 번호를 함께 돌려준다(개수 상한에 먼저 닿으면 `None`)."""
    out: list[str] = []
    current: str | None = None
    mismatch = False
    fence: _Fence | None = None
    fence_line = -1

    def flush() -> bool:
        if current is not None and (mismatch or not only_mismatch):
            out.append(current)
        return len(out) >= limit

    for number, line in enumerate(lines):
        marker = None if plain is None or number in plain else _fence_of(line)
        if fence is not None:
            if (
                marker is not None
                and marker.char == fence.char
                and marker.length >= fence.length
                and not marker.rest.strip()
            ):
                fence = None
            continue
        if marker is not None and not (marker.char == "`" and "`" in marker.rest):
            fence = marker
            fence_line = number
            continue
        if line.startswith(_HEADING):
            if flush():
                return out, None
            title = _title(line[len(_HEADING) :])
            current = title or None  # 빈 제목은 항목이 아니다
            mismatch = False
        elif line.startswith(("# ", "## ")):
            if flush():
                return out, None
            current = None
            mismatch = False
        elif current is not None and not mismatch and _is_mismatch_line(line):
            mismatch = True
    if flush():
        return out, None
    return out, fence_line if fence is not None else None


def _split_lines(text: str) -> list[str]:
    # `\r\n`과 단독 `\r`도 줄바꿈으로 본다. 정규식 없이 선형이다.
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _parse(text: str, limit: int, only_mismatch: bool) -> list[str]:
    lines = _split_lines(text)
    plain: set[int] = set()
    for _ in range(_MAX_RESCANS):
        titles, opener = _parse_once(lines, frozenset(plain), limit, only_mismatch)
        if opener is None:
            return titles
        plain.add(opener)  # 짝 없는 펜스 하나를 일반 글로 보고 다시 읽는다
    return _parse_once(lines, None, limit, only_mismatch)[0]


def question_headings(text: str) -> list[str]:
    """`### ` 제목을 모두(최대 `MAX_OQ_ITEMS`개) 뽑는다. 빈 제목은 뺀다."""
    return _parse(text, MAX_OQ_ITEMS, only_mismatch=False)


def mismatch_titles(text: str) -> list[str]:
    """`- 종류: 어긋남` 항목의 제목만(최대 `MAX_OQ_ITEMS`개) 뽑는다."""
    return _parse(text, MAX_OQ_ITEMS, only_mismatch=True)


class _Snapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    titles: Annotated[list[str], FailFast(), Field(max_length=MAX_OQ_ITEMS)]


_SNAPSHOT_ADAPTER: Final[TypeAdapter[_Snapshot]] = TypeAdapter(_Snapshot)


def _fit_snapshot(titles: list[str]) -> list[str]:
    """스냅샷 파일이 `MAX_STATE_JSON` 안에 읽히도록 앞에서부터 들어가는 만큼만 남긴다.

    (제목은 최대 300자라 4바이트 문자만 있으면 1000개가 1MiB를 넘을 수 있다.) 추적 대상 자체를 이
    목록으로 줄이므로, 못 담은 제목이 매번 신규로 잡히는 일은 없다.
    """
    kept: list[str] = []
    used = 0
    for title in titles:
        used += len(json.dumps(title, ensure_ascii=False).encode("utf-8")) + 8  # 들여쓰기·쉼표·여유
        if used > _SNAPSHOT_BUDGET:
            break
        kept.append(title)
    return kept


def track_open_questions(data_dir: Path, state_dir: Path, now: datetime) -> list[OqEventRecord]:
    """지난 스냅샷과 비교해 어긋남 미결의 신규·해소를 `oq-events.jsonl`에 덧붙이고 돌려준다.

    스냅샷이 없거나 깨졌으면 빈 것으로 본다. 스냅샷의 제목도 믿지 않고 같은 `_title`로 다시 다듬는다
    (멱등이라 자기가 쓴 것은 그대로다). 스냅샷에 다 못 담는 만큼(드문 거대 입력)은 추적하지 않는다.
    기록을 먼저 쓰고 스냅샷을 나중에 쓰므로, 중간에 실패하면 다음 실행이 같은 사건을 한 번 더 남길
    수는 있어도 놓치지는 않는다. 쓰기 실패(`OSError`)는 그대로 올라간다.
    """
    text = read_text_if_exists(data_dir / "wiki" / "open-questions.md")
    current = _fit_snapshot(list(dict.fromkeys(mismatch_titles(text))))
    snapshot_file = state_dir / STATE_FILES.oq_snapshot
    saved = read_json(snapshot_file, _SNAPSHOT_ADAPTER, _Snapshot(titles=[]))
    previous = [t for t in dict.fromkeys(_title(t) for t in saved.titles) if t]
    current_set = set(current)
    previous_set = set(previous)
    at = kst_iso(now)
    events = [
        *(OqEventRecord(at=at, kind="opened", title=t) for t in current if t not in previous_set),
        *(OqEventRecord(at=at, kind="resolved", title=t) for t in previous if t not in current_set),
    ]
    for event in events:
        append_jsonl(state_dir / STATE_FILES.oq_events, event)
    write_json(snapshot_file, _Snapshot(titles=current))
    return events
