"""KST 기준 ISO 주와 주간 측정 요약.

요약에는 개수·비용·시간·비율만 넣는다. 질문·미결 제목 같은 원문과 사용자 이름은 싣지 않는다
(위키 저장소에 커밋되므로). 기록 파일은 믿을 수 없는 입력이다: 읽기는 `read_jsonl`(검증을 통과하지
못한 줄은 이미 빠져 있다)을 거치고, 읽을 수 없는 시각의 기록은 건너뛴다.
"""

import logging
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from team_wiki_engine.metrics.record import (
    STATE_FILES,
    OqEventRecord,
    PrReviewRecord,
    QuestionRecord,
    SyncRunRecord,
    read_jsonl_window,
)
from team_wiki_engine.util.time import KST, kst_parts, parse_instant, to_kst

_LABEL: Final = re.compile(r"[0-9]{4}-W[0-9]{2}")
"""주 라벨. 반드시 `fullmatch`로 쓴다. 경로와 제목에 들어가기 전의 검사다."""
_WEEK: Final = timedelta(days=7)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeekRange:
    label: str
    start: datetime  # 월요일 00:00 KST(aware)
    end: datetime  # 다음 월요일 00:00 KST(제외)


@dataclass(frozen=True)
class WeeklyDue:
    label: str
    rel_path: str
    content: str


def iso_week(d: datetime) -> WeekRange:
    """`d`(naive는 UTC)가 속한 KST ISO 주. 한국은 서머타임이 없어 7일이 정확히 7일이다."""
    local = to_kst(d)
    year, week, _ = local.date().isocalendar()
    monday = local.date() - timedelta(days=local.weekday())
    start = datetime(monday.year, monday.month, monday.day, tzinfo=KST)
    return WeekRange(label=f"{year:04d}-W{week:02d}", start=start, end=start + _WEEK)


def previous_week(now: datetime) -> WeekRange:
    return iso_week(now - _WEEK)


class _Timed(BaseModel):
    at: str


def _in_week[R: BaseModel](
    records: Sequence[R], week: WeekRange, at_of: Callable[[R], str]
) -> list[R]:
    kept: list[R] = []
    for record in records:
        at = parse_instant(at_of(record))
        if at is not None and week.start <= at < week.end:
            kept.append(record)
    return kept


def _load[R: BaseModel](
    state_dir: Path, name: str, model: type[R], week: WeekRange, at_of: Callable[[R], str]
) -> list[R] | None:
    """그 주의 기록. 누적 파일이 읽기 상한을 넘어 앞부분을 건너뛰었는데 읽은 부분이 주 시작보다 앞선
    기록까지 닿지 않으면(주 전체가 들어 있는지 알 수 없다) `None`이다. 기록은 시간 순으로 덧붙는다고
    본다."""
    window = read_jsonl_window(state_dir / name, model)
    if window.truncated:
        first = next(
            (t for t in (parse_instant(at_of(r)) for r in window.records) if t is not None), None
        )
        if first is None or first >= week.start:
            logger.warning(
                "%s 앞부분을 읽지 않아 %s 주를 다 덮는지 몰라 측정 요약을 만들지 않습니다",
                name,
                week.label,
            )
            return None
    return _in_week(window.records, week, at_of)


def _money(total: float, digits: int) -> str:
    """합이 무한대로 넘쳤으면 `-`(사람이 손으로 넣은 터무니없는 값 방어)."""
    return f"{total:.{digits}f}" if total == total and abs(total) != float("inf") else "-"


def _percentile(sorted_values: Sequence[int], percent: int) -> int | None:
    if not sorted_values:
        return None
    rank = (percent * len(sorted_values) + 99) // 100  # 올림, 정수 계산
    return sorted_values[min(len(sorted_values) - 1, max(rank, 1) - 1)]


def _seconds(ms: int | None) -> str:
    return "-" if ms is None else f"{ms / 1000:.1f}초"


def build_weekly_summary(state_dir: Path, week: WeekRange, now: datetime) -> str | None:
    """그 주의 기록으로 표를 만든다. 기록이 하나도 없거나 라벨 모양이 이상하면 `None`.

    기록 파일은 누적되므로 끝에서부터(`read_jsonl_window`) 읽는다. 파일이 읽기 상한보다 커서
    앞부분을 건너뛰었고 읽은 부분이 주 시작 이전 기록까지 닿지 않으면, 일부만 센 숫자를 쓰지
    않도록 `None`이다(내용 없는 경고를 남긴다).
    """
    if _LABEL.fullmatch(week.label) is None:
        return None
    questions = _load(state_dir, STATE_FILES.questions, QuestionRecord, week, lambda r: r.at)
    runs = _load(state_dir, STATE_FILES.sync_runs, SyncRunRecord, week, lambda r: r.at)
    reviews = _load(state_dir, STATE_FILES.pr_review, PrReviewRecord, week, lambda r: r.at)
    oq = _load(state_dir, STATE_FILES.oq_events, OqEventRecord, week, lambda r: r.at)
    if questions is None or runs is None or reviews is None or oq is None:
        return None
    if not (questions or runs or reviews or oq):
        return None

    ok = [q for q in questions if q.ok]
    wiki_only = sum(1 for q in ok if q.source == "wiki" and not q.not_found)
    durations = sorted(q.duration_ms for q in ok)
    question_cost = sum(q.cost_usd for q in questions)
    sync_cost = sum(r.cost_usd for r in runs)
    merged = [r for r in reviews if r.outcome == "merged"]
    unknown = sum(1 for r in merged if r.human_changed_files is None)
    files = sum(r.human_changed_files or 0 for r in merged)
    additions = sum(r.human_additions or 0 for r in merged)
    deletions = sum(r.human_deletions or 0 for r in merged)
    opened = sum(1 for e in oq if e.kind == "opened")
    resolved = len(oq) - opened

    share = "-" if not ok else f"{wiki_only / len(ok) * 100:.1f}% ({wiki_only}/{len(ok)})"
    average = "-" if not questions else _money(question_cost / len(questions), 3)
    cost_cell = "-" if average == "-" else f"${average}"
    sync_total = _money(sync_cost, 2)
    sync_cell = "-" if sync_total == "-" else f"${sync_total}"
    human = f"{files} / +{additions} -{deletions}" + (
        f" (측정 불가 {unknown}건)" if unknown else ""
    )
    rows = [
        ("질문 수", f"{len(questions)} (실패 {len(questions) - len(ok)})"),
        ("위키만으로 답한 비율", share),
        (
            "응답 시간 중앙값 / p90",
            f"{_seconds(_percentile(durations, 50))} / {_seconds(_percentile(durations, 90))}",
        ),
        ("질문당 평균 비용", cost_cell),
        ("동기화 비용 합계", f"{sync_cell} ({len(runs)}회)"),
        ("어긋남 미결 신규 / 해소", f"{opened} / {resolved}"),
        ("PR 머지 / 거절", f"{len(merged)} / {len(reviews) - len(merged)}"),
        ("사람이 고친 파일 / 줄", human),
    ]
    first = kst_parts(week.start).date
    last = kst_parts(week.end - timedelta(seconds=1)).date
    return "\n".join(
        [
            "---",
            f"updated: {kst_parts(now).date}",
            "---",
            "",
            f"# {week.label} 측정 요약 ({first} ~ {last})",
            "",
            "| 항목 | 값 |",
            "|---|---|",
            *(f"| {key} | {value} |" for key, value in rows),
            "",
        ]
    )


def weekly_due(data_dir: Path, state_dir: Path, now: datetime) -> WeeklyDue | None:
    """지난주 요약을 써야 하면 내용을 돌려준다. 쓰지는 않는다(쓰기는 호출자의 원자적 커밋 몫).

    `metrics/weekly/<라벨>.md`가 파일·폴더·링크(깨진 것 포함)·파이프 등 **어떤 형태로든** 있으면
    `None`이다(덮어쓰거나 링크를 따라가지 않는다). 라벨이 `[0-9]{4}-W[0-9]{2}`가 아니어도
    `None`이다.
    """
    week = previous_week(now)
    if _LABEL.fullmatch(week.label) is None:
        return None
    rel_path = f"metrics/weekly/{week.label}.md"
    if os.path.lexists(data_dir / rel_path):
        return None
    content = build_weekly_summary(state_dir, week, now)
    return (
        None if content is None else WeeklyDue(label=week.label, rel_path=rel_path, content=content)
    )
