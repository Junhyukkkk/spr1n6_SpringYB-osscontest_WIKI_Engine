"""ISO 주(KST)와 주간 요약. 상태 파일은 믿을 수 없는 입력이다."""

import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.metrics.record import (
    MAX_JSONL_FILE,
    STATE_FILES,
    OqEventRecord,
    PrReviewRecord,
    QuestionRecord,
    SyncRunRecord,
    append_jsonl,
)
from team_wiki_engine.metrics.weekly import (
    WeeklyDue,
    WeekRange,
    build_weekly_summary,
    iso_week,
    previous_week,
    weekly_due,
)
from team_wiki_engine.util.time import KST, kst_iso
from tests.helpers import write_tree
from tests.helpers.hang_guard import hang_guard

W41 = iso_week(datetime(2026, 10, 7, 0, 0, tzinfo=UTC))
NOW = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)


class TestIsoWeek:
    def test_주_번호와_범위를_계산한다(self) -> None:
        w = iso_week(datetime(2026, 10, 7, 3, 0, tzinfo=UTC))
        assert w.label == "2026-W41"
        assert w.start == datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
        assert kst_iso(w.start) == "2026-10-05T00:00:00+09:00"
        assert w.start.utcoffset() == timedelta(hours=9)
        assert w.end == datetime(2026, 10, 11, 15, 0, tzinfo=UTC)
        assert w.end - w.start == timedelta(days=7)

    def test_연초와_연말_라벨(self) -> None:
        assert iso_week(datetime(2026, 1, 1, 3, 0, tzinfo=UTC)).label == "2026-W01"  # 목요일
        assert iso_week(datetime(2027, 1, 1, 3, 0, tzinfo=UTC)).label == "2026-W53"
        assert iso_week(datetime(2025, 12, 29, 0, 0, tzinfo=KST)).label == "2026-W01"

    def test_2026_W53은_12월_28일_월요일에서_1월_3일_일요일까지(self) -> None:
        w = iso_week(datetime(2026, 12, 28, 0, 0, tzinfo=KST))
        assert w.label == "2026-W53"
        assert kst_iso(w.start) == "2026-12-28T00:00:00+09:00"
        sunday = iso_week(datetime(2027, 1, 3, 23, 59, 59, tzinfo=KST))
        assert sunday.label == "2026-W53"
        assert iso_week(datetime(2027, 1, 4, 0, 0, tzinfo=KST)).label == "2027-W01"
        assert iso_week(datetime(2026, 12, 27, 23, 59, 59, tzinfo=KST)).label == "2026-W52"

    def test_KST_월요일_0시가_경계다(self) -> None:
        assert iso_week(datetime(2026, 10, 11, 14, 59, 59, tzinfo=UTC)).label == "2026-W41"
        assert iso_week(datetime(2026, 10, 11, 15, 0, 0, tzinfo=UTC)).label == "2026-W42"

    def test_aware와_naive가_같은_순간이면_같은_주다(self) -> None:
        naive = datetime(2026, 10, 11, 15, 0)  # naive는 UTC
        aware_utc = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)
        aware_kst = datetime(2026, 10, 12, 0, 0, tzinfo=KST)
        aware_la = datetime(2026, 10, 11, 8, 0, tzinfo=timezone(timedelta(hours=-7)))
        weeks = {iso_week(d) for d in (naive, aware_utc, aware_kst, aware_la)}
        assert len(weeks) == 1
        assert next(iter(weeks)).label == "2026-W42"

    def test_지난주를_돌려준다(self) -> None:
        assert previous_week(datetime(2026, 10, 12, 0, 0, tzinfo=UTC)).label == "2026-W41"
        assert previous_week(datetime(2027, 1, 4, 0, 0, tzinfo=KST)).label == "2026-W53"
        assert previous_week(datetime(2026, 1, 5, 12, 0)).label == "2026-W01"

    def test_일요일_밤의_지난주는_바로_앞_주다(self) -> None:
        sunday = datetime(2026, 10, 11, 23, 59, 59, tzinfo=KST)
        assert iso_week(sunday).label == "2026-W41"
        assert previous_week(sunday).label == "2026-W40"
        assert previous_week(datetime(2026, 10, 11, 15, 0, tzinfo=UTC)).label == "2026-W41"

    def test_범위는_이어지고_일곱_날이다(self) -> None:
        a, b = (
            iso_week(datetime(2026, 10, 7, tzinfo=UTC)),
            iso_week(datetime(2026, 10, 14, tzinfo=UTC)),
        )
        assert a.end == b.start
        assert isinstance(a, WeekRange)


def _seed_question(
    state: Path,
    at: str,
    ok: bool,
    source: Any,
    duration_ms: int,
    cost: float,
    not_found: bool = False,
) -> None:
    append_jsonl(
        state / STATE_FILES.questions,
        QuestionRecord(
            at=at,
            user="u",
            thread_id="t",
            ok=ok,
            failure=None if ok else "timeout",
            source=source,
            duration_ms=duration_ms,
            cost_usd=cost,
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            resumed=False,
            not_found=not_found,
        ),
    )


def _seed_run(state: Path, at: str, run_id: str, kind: Any, result: Any, cost: float) -> None:
    append_jsonl(
        state / STATE_FILES.sync_runs,
        SyncRunRecord(
            at=at,
            run_id=run_id,
            kind=kind,
            result=result,
            pr="1" if result == "pr" else None,
            lint_failed=False,
            cost_usd=cost,
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
        ),
    )


def _seed_review(
    state: Path,
    at: str,
    pr: str,
    outcome: Any,
    files: int | None,
    add: int | None,
    dele: int | None,
) -> None:
    append_jsonl(
        state / STATE_FILES.pr_review,
        PrReviewRecord(
            at=at,
            pr=pr,
            kind="code",
            outcome=outcome,
            human_changed_files=files,
            human_additions=add,
            human_deletions=dele,
            closed_at=None,
        ),
    )


def seed_records(state: Path) -> None:
    _seed_question(state, "2026-10-05T10:00:00+09:00", True, "wiki", 10_000, 0.05)
    _seed_question(state, "2026-10-06T10:00:00+09:00", True, "code", 20_000, 0.1)
    _seed_question(state, "2026-10-07T10:00:00+09:00", True, "wiki", 40_000, 0.06)
    _seed_question(state, "2026-10-08T10:00:00+09:00", False, None, 300_000, 0.01)
    _seed_question(state, "2026-10-13T10:00:00+09:00", True, "wiki", 1_000, 9)  # 다음 주
    _seed_run(state, "2026-10-06T09:00:00+09:00", "r1", "code", "pr", 0.4)
    _seed_run(state, "2026-10-06T21:00:00+09:00", "r2", "ingest", "no-change", 0)
    _seed_review(state, "2026-10-07T09:00:00+09:00", "1", "merged", 2, 5, 1)
    _seed_review(state, "2026-10-08T09:00:00+09:00", "2", "rejected", None, None, None)
    append_jsonl(
        state / STATE_FILES.oq_events,
        OqEventRecord(at="2026-10-07T09:00:00+09:00", kind="opened", title="a"),
    )


class TestBuildWeeklySummary:
    def test_지난주_기록으로_표를_만든다(self, tmp_dir: Path) -> None:
        seed_records(tmp_dir)
        text = build_weekly_summary(tmp_dir, W41, NOW)
        assert text is not None
        assert text.startswith("---\nupdated: 2026-10-12\n---\n")
        assert "# 2026-W41 측정 요약 (2026-10-05 ~ 2026-10-11)" in text
        assert "| 질문 수 | 4 (실패 1) |" in text
        assert "| 위키만으로 답한 비율 | 66.7% (2/3) |" in text
        assert "| 응답 시간 중앙값 / p90 | 20.0초 / 40.0초 |" in text
        assert "| 질문당 평균 비용 | $0.055 |" in text
        assert "| 동기화 비용 합계 | $0.40 (2회) |" in text
        assert "| 어긋남 미결 신규 / 해소 | 1 / 0 |" in text
        assert "| PR 머지 / 거절 | 1 / 1 |" in text
        assert "| 사람이 고친 파일 / 줄 | 2 / +5 -1 |" in text
        assert text.endswith("|\n")

    def test_찾지_못함_위키_답은_비율_분자에서_빠진다(self, tmp_dir: Path) -> None:
        _seed_question(tmp_dir, "2026-10-07T09:00:00+09:00", True, "wiki", 1000, 0.0)
        _seed_question(tmp_dir, "2026-10-07T10:00:00+09:00", True, "wiki", 1000, 0.0, True)
        text = build_weekly_summary(tmp_dir, W41, NOW)
        assert text is not None
        assert "| 위키만으로 답한 비율 | 50.0% (1/2) |" in text

    def test_기록이_없으면_None이다(self, tmp_dir: Path) -> None:
        assert build_weekly_summary(tmp_dir, W41, NOW) is None

    def test_측정_불가_머지는_건수를_덧붙인다(self, tmp_dir: Path) -> None:
        _seed_review(tmp_dir, "2026-10-07T09:00:00+09:00", "1", "merged", 1, 2, 3)
        _seed_review(tmp_dir, "2026-10-07T10:00:00+09:00", "2", "merged", None, None, None)
        text = build_weekly_summary(tmp_dir, W41, NOW)
        assert text is not None
        assert "| 사람이 고친 파일 / 줄 | 1 / +2 -3 (측정 불가 1건) |" in text

    def test_질문이_없으면_비율과_평균은_대시(self, tmp_dir: Path) -> None:
        _seed_run(tmp_dir, "2026-10-06T09:00:00+09:00", "r1", "code", "no-change", 0)
        text = build_weekly_summary(tmp_dir, W41, NOW)
        assert text is not None
        assert "| 질문 수 | 0 (실패 0) |" in text
        assert "| 위키만으로 답한 비율 | - |" in text
        assert "| 응답 시간 중앙값 / p90 | - / - |" in text
        assert "| 질문당 평균 비용 | - |" in text

    def test_주_경계는_시작_포함_끝_제외다(self, tmp_dir: Path) -> None:
        _seed_question(tmp_dir, "2026-10-05T00:00:00+09:00", True, "wiki", 1000, 0.0)  # 포함
        _seed_question(tmp_dir, "2026-10-11T23:59:59+09:00", True, "wiki", 1000, 0.0)  # 포함
        _seed_question(tmp_dir, "2026-10-12T00:00:00+09:00", True, "wiki", 1000, 0.0)  # 제외
        _seed_question(tmp_dir, "2026-10-04T23:59:59+09:00", True, "wiki", 1000, 0.0)  # 제외
        text = build_weekly_summary(tmp_dir, W41, NOW)
        assert text is not None
        assert "| 질문 수 | 2 (실패 0) |" in text

    def test_UTC_Z와_naive_시각도_읽는다(self, tmp_dir: Path) -> None:
        _seed_question(tmp_dir, "2026-10-04T15:00:00Z", True, "wiki", 1000, 0.0)  # KST 월 0시
        _seed_question(tmp_dir, "2026-10-11T14:59:59", True, "wiki", 1000, 0.0)  # naive는 UTC
        _seed_question(tmp_dir, "2026-10-11T15:00:00Z", True, "wiki", 1000, 0.0)  # 다음 주
        text = build_weekly_summary(tmp_dir, W41, NOW)
        assert text is not None
        assert "| 질문 수 | 2 (실패 0) |" in text

    def test_NaN_음수_비용과_읽을_수_없는_시각은_집계에서_빠진다(self, tmp_dir: Path) -> None:
        _seed_question(tmp_dir, "2026-10-05T10:00:00+09:00", True, "wiki", 10_000, 0.5)
        base = (
            '{{"at": {at}, "user": "u", "thread_id": "t", "ok": true, "failure": null, '
            '"source": "wiki", "duration_ms": 1, "cost_usd": {cost}, "input_tokens": 0, '
            '"output_tokens": 0, "cache_read_input_tokens": 0, "resumed": false}}\n'
        )
        ok_at = '"2026-10-06T10:00:00+09:00"'
        lines = [
            base.format(at=ok_at, cost="NaN"),
            base.format(at=ok_at, cost="-1"),
            base.format(at=ok_at, cost="-Infinity"),
            base.format(at='"어제"', cost="0.1"),
            base.format(at="12", cost="0.1"),
            base.format(at='"9999-12-31T23:59:59-23:59"', cost="0.1"),
        ]
        with (tmp_dir / STATE_FILES.questions).open("a", encoding="utf-8") as file:
            file.writelines(lines)
        text = build_weekly_summary(tmp_dir, W41, NOW)
        assert text is not None
        assert "| 질문 수 | 1 (실패 0) |" in text
        assert "| 질문당 평균 비용 | $0.500 |" in text
        assert "nan" not in text.lower()

    def test_읽을_수_없는_시각만_있으면_None(self, tmp_dir: Path) -> None:
        (tmp_dir / STATE_FILES.oq_events).write_text(
            '{"at": "어제", "kind": "opened", "title": "x"}\n', encoding="utf-8"
        )
        assert build_weekly_summary(tmp_dir, W41, NOW) is None

    def test_비용이_너무_커서_합이_무한대가_되면_대시로_보인다(self, tmp_dir: Path) -> None:
        for i in range(2):
            _seed_question(tmp_dir, f"2026-10-0{5 + i}T10:00:00+09:00", True, "wiki", 1, 1e308)
            _seed_run(tmp_dir, f"2026-10-0{5 + i}T10:00:00+09:00", f"r{i}", "code", "pr", 1e308)
        text = build_weekly_summary(tmp_dir, W41, NOW)
        assert text is not None
        assert "inf" not in text.lower()
        assert "| 질문당 평균 비용 | - |" in text
        assert "| 동기화 비용 합계 | - (2회) |" in text

    def test_질문_원문과_사용자는_요약에_없다(self, tmp_dir: Path) -> None:
        seed_records(tmp_dir)
        append_jsonl(
            tmp_dir / STATE_FILES.oq_events,
            OqEventRecord(
                at="2026-10-07T10:00:00+09:00", kind="opened", title="비밀 질문 원문 @everyone"
            ),
        )
        text = build_weekly_summary(tmp_dir, W41, NOW)
        assert text is not None
        assert "비밀 질문" not in text
        assert "@everyone" not in text
        assert "| 어긋남 미결 신규 / 해소 | 2 / 0 |" in text

    def test_이상한_라벨이면_None이다(self, tmp_dir: Path) -> None:
        seed_records(tmp_dir)
        bad = WeekRange(label="2026-W41\n# 주입", start=W41.start, end=W41.end)
        assert build_weekly_summary(tmp_dir, bad, NOW) is None

    def test_상태_파일이_망가져도_예외가_없다(self, tmp_dir: Path) -> None:
        (tmp_dir / STATE_FILES.questions).write_bytes(b"\xff\x00{{{\n" * 100)
        (tmp_dir / STATE_FILES.sync_runs).mkdir()
        os.mkfifo(tmp_dir / STATE_FILES.pr_review)
        (tmp_dir / STATE_FILES.oq_events).symlink_to(tmp_dir / "없음")
        with hang_guard(10):
            assert build_weekly_summary(tmp_dir, W41, NOW) is None

    def test_now가_naive여도_KST_날짜로_쓴다(self, tmp_dir: Path) -> None:
        seed_records(tmp_dir)
        text = build_weekly_summary(tmp_dir, W41, datetime(2026, 10, 11, 15, 0))
        assert text is not None
        assert "updated: 2026-10-12" in text


class TestWeeklyDue:
    def test_지난주_요약이_없고_기록이_있으면_만든다_이미_있으면_None(self, tmp_dir: Path) -> None:
        state, data = tmp_dir / "state", tmp_dir / "data"
        seed_records(state)
        due = weekly_due(data, state, NOW)
        assert isinstance(due, WeeklyDue)
        assert due.label == "2026-W41"
        assert due.rel_path == "metrics/weekly/2026-W41.md"
        assert due.content == build_weekly_summary(state, W41, NOW)
        write_tree(data, {due.rel_path: due.content})
        assert weekly_due(data, state, NOW) is None

    def test_기록이_없으면_None(self, tmp_dir: Path) -> None:
        assert weekly_due(tmp_dir / "data", tmp_dir / "state", NOW) is None

    @pytest.mark.parametrize("kind", ["file", "dir", "dangling", "link", "fifo"])
    def test_어떤_형태로든_같은_이름이_있으면_None(self, tmp_dir: Path, kind: str) -> None:
        state, data = tmp_dir / "state", tmp_dir / "data"
        seed_records(state)
        weekly = data / "metrics" / "weekly"
        weekly.mkdir(parents=True)
        target = weekly / "2026-W41.md"
        if kind == "file":
            target.write_text("x", encoding="utf-8")
        elif kind == "dir":
            target.mkdir()
        elif kind == "dangling":
            target.symlink_to(tmp_dir / "없음")
        elif kind == "link":
            (tmp_dir / "real.md").write_text("x", encoding="utf-8")
            target.symlink_to(tmp_dir / "real.md")
        else:
            os.mkfifo(target)
        with hang_guard(10):
            assert weekly_due(data, state, NOW) is None

    def test_다른_주의_파일은_영향이_없다(self, tmp_dir: Path) -> None:
        state, data = tmp_dir / "state", tmp_dir / "data"
        seed_records(state)
        write_tree(data, {"metrics/weekly/2026-W40.md": "x"})
        due = weekly_due(data, state, NOW)
        assert due is not None
        assert due.label == "2026-W41"

    def test_경로는_검증한_라벨로만_만든다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import team_wiki_engine.metrics.weekly as weekly

        state, data = tmp_dir / "state", tmp_dir / "data"
        seed_records(state)
        for label in ("2026-W41\n", "../x", "2026-W4", "２０２６-W41", "2026-W41/../../x"):
            monkeypatch.setattr(
                weekly, "previous_week", lambda _now, lab=label: WeekRange(lab, W41.start, W41.end)
            )
            assert weekly.weekly_due(data, state, NOW) is None


def _fill(path: Path, lines: list[str]) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _question_line(at: str, index: int) -> str:
    return QuestionRecord(
        at=at,
        user="u" + "x" * 150,
        thread_id=f"t{index}",
        ok=True,
        failure=None,
        source="wiki",
        duration_ms=1000,
        cost_usd=0.5,
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        resumed=False,
    ).model_dump_json()


def _lines_over_cap(at: str) -> list[str]:
    line = _question_line(at, 0)
    count = MAX_JSONL_FILE // (len(line) + 1) + 2000
    return [line] * count


class TestCumulativeFiles:
    def test_8MiB를_넘는_누적_파일이어도_이번_주가_맞다(self, tmp_dir: Path) -> None:
        old = _lines_over_cap("2026-01-05T10:00:00+09:00")
        current = [_question_line("2026-10-06T10:00:00+09:00", i) for i in range(3)]
        _fill(tmp_dir / STATE_FILES.questions, [*old, *current])
        assert (tmp_dir / STATE_FILES.questions).stat().st_size > MAX_JSONL_FILE
        text = build_weekly_summary(tmp_dir, W41, NOW)
        assert text is not None
        assert "| 질문 수 | 3 (실패 0) |" in text
        assert "| 질문당 평균 비용 | $0.500 |" in text

    def test_끝부분이_주_시작에_못_미치면_숫자를_쓰지_않고_None(
        self, tmp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _fill(tmp_dir / STATE_FILES.questions, _lines_over_cap("2026-10-06T10:00:00+09:00"))
        with caplog.at_level("WARNING"):
            assert build_weekly_summary(tmp_dir, W41, NOW) is None
        assert any("측정" in r.getMessage() for r in caplog.records)
        assert all("xxxx" not in r.getMessage() for r in caplog.records)  # 기록 내용을 싣지 않는다

    def test_weekly_due도_일부만_덮는_기록으로는_만들지_않는다(self, tmp_dir: Path) -> None:
        state, data = tmp_dir / "state", tmp_dir / "data"
        state.mkdir()
        _fill(state / STATE_FILES.questions, _lines_over_cap("2026-10-06T10:00:00+09:00"))
        assert weekly_due(data, state, NOW) is None

    def test_큰_파일의_끝이_읽을_수_없는_시각뿐이면_None(self, tmp_dir: Path) -> None:
        _fill(tmp_dir / STATE_FILES.questions, _lines_over_cap("어제"))
        assert build_weekly_summary(tmp_dir, W41, NOW) is None

    def test_작은_파일은_첫_기록이_주_안에_있어도_쓴다(self, tmp_dir: Path) -> None:
        seed_records(tmp_dir)
        assert build_weekly_summary(tmp_dir, W41, NOW) is not None
