"""미결 파싱·추적. 파일은 믿을 수 없는 입력이므로 적대적 모양도 본다."""

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from team_wiki_engine.metrics.oq import (
    MAX_OQ_ITEMS,
    MAX_TITLE_CHARS,
    _title,
    mismatch_titles,
    question_headings,
    track_open_questions,
)
from team_wiki_engine.metrics.record import (
    MAX_STATE_JSON,
    STATE_FILES,
    OqEventRecord,
    read_jsonl,
)
from tests.helpers import write_tree
from tests.helpers.hang_guard import hang_guard


def _oq(items: str) -> str:
    return f"---\nupdated: 2026-10-07\n---\n\n# 미결\n\n{items}"


def _item(title: str, kind: str) -> str:
    return f"### {title}\n- 종류: {kind}\n- 내용: x\n\n"


def _write_oq(data: Path, text: str) -> None:
    write_tree(data, {"wiki/open-questions.md": text})


class TestParsing:
    def test_모든_제목을_뽑는다(self) -> None:
        text = _oq(_item("[2026-10-06] a", "어긋남") + _item("[2026-10-07] b", "미정"))
        assert question_headings(text) == ["[2026-10-06] a", "[2026-10-07] b"]

    def test_종류가_어긋남인_항목만_뽑는다(self) -> None:
        text = _oq(
            _item("[2026-10-06] a", "어긋남")
            + _item("[2026-10-07] b", "미정")
            + _item("[2026-10-08] c", "어긋남")
        )
        assert mismatch_titles(text) == ["[2026-10-06] a", "[2026-10-08] c"]

    def test_상위_제목에서_항목이_끊긴다(self) -> None:
        text = "### a\n## 다른 절\n- 종류: 어긋남\n"
        assert mismatch_titles(text) == []
        assert question_headings(text) == ["a"]

    def test_펜스_안의_가짜_제목은_제목이_아니다(self) -> None:
        text = _oq(
            _item("진짜", "미정")
            + "```\n### 가짜\n- 종류: 어긋남\n```\n"
            + _item("진짜2", "어긋남")
        )
        assert question_headings(text) == ["진짜", "진짜2"]
        assert mismatch_titles(text) == ["진짜2"]

    def test_펜스_안의_종류_줄은_항목에_영향을_주지_않는다(self) -> None:
        text = "### a\n```\n- 종류: 어긋남\n```\n"
        assert mismatch_titles(text) == []

    def test_펜스는_같은_종류의_더_긴_줄로만_닫힌다(self) -> None:
        text = "````\n### 가짜1\n```\n### 가짜2\n~~~~\n### 가짜3\n````\n### 진짜\n"
        assert question_headings(text) == ["진짜"]

    def test_닫히지_않은_펜스는_여는_줄을_일반_글로_보고_다시_읽는다(self) -> None:
        text = "### 앞\n- 종류: 어긋남\n```\n### 뒤\n- 종류: 어긋남\n"
        assert question_headings(text) == ["앞", "뒤"]
        assert mismatch_titles(text) == ["앞", "뒤"]

    @pytest.mark.parametrize("opener", ["```", "~~~", "````", "~~~~~ x"])
    def test_낱개_펜스_뒤의_항목도_계속_추적한다(self, opener: str) -> None:
        text = _oq(opener + "\n" + "".join(_item(f"q{i}", "어긋남") for i in range(3)))
        assert question_headings(text) == ["q0", "q1", "q2"]
        assert mismatch_titles(text) == ["q0", "q1", "q2"]

    def test_짝이_맞는_펜스는_여전히_안을_숨긴다(self) -> None:
        text = "```\n### 가짜\n```\n### 진짜\n```\n### 가짜2\n"
        assert question_headings(text) == ["진짜", "가짜2"]  # 마지막 ```만 짝이 없다

    def test_낱개_펜스를_고치지_않은_채_여러_개여도_끝난다(self) -> None:
        text = "````\n```\n" * 5 + "### 끝\n"
        assert question_headings(text) == ["끝"]

    def test_낱개_펜스는_스냅샷과_엇갈려_해소로_기록되지_않는다(self, tmp_dir: Path) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        items = "".join(_item(f"q{i}", "어긋남") for i in range(3))
        _write_oq(data, _oq(items))
        now = datetime(2026, 10, 7, tzinfo=UTC)
        assert len(track_open_questions(data, state, now)) == 3
        _write_oq(data, _oq("```\n" + items))
        assert track_open_questions(data, state, now) == []
        _write_oq(data, _oq(items))
        assert track_open_questions(data, state, now) == []

    def test_들여쓰기가_깊거나_정보_문자열에_백틱이_있으면_펜스가_아니다(self) -> None:
        text = "    ```\n### a\n``` x`y\n### b\n"
        assert question_headings(text) == ["a", "b"]

    def test_CRLF_파일(self) -> None:
        text = _oq(_item("a", "어긋남") + _item("b", "미정")).replace("\n", "\r\n")
        assert question_headings(text) == ["a", "b"]
        assert mismatch_titles(text) == ["a"]

    def test_제어_문자가_든_제목은_이스케이프한다(self) -> None:
        titles = question_headings("### a\x1b[31m‮b\tc\n")
        assert titles == ["a\\x1b[31m\\u202eb\\tc"]

    def test_제목은_이스케이프한_뒤_300자로_자른다(self) -> None:
        [title] = question_headings("### " + "\x1b" * 1000 + "\n")
        assert len(title) == MAX_TITLE_CHARS
        assert title.startswith("\\x1b\\x1b")
        [plain] = question_headings("### " + "가" * 1000 + "\n")
        assert plain == "가" * MAX_TITLE_CHARS

    def test_항목은_1000개까지(self) -> None:
        text = "".join(_item(f"q{i}", "어긋남") for i in range(1500))
        assert len(question_headings(text)) == MAX_OQ_ITEMS == 1000
        assert len(mismatch_titles(text)) == MAX_OQ_ITEMS
        assert question_headings(text)[-1] == "q999"

    def test_적대적_입력도_선형_시간에_끝난다(self) -> None:
        hostile = [
            " " * 2_000_000 + "-",
            "### " + "a" * 2_000_000,
            "```" * 500_000,
            "`" * 2_000_000,
            "### a\n" + "- " + " " * 1_000_000 + "종류:" + " " * 1_000_000 + "\n",
            "\x00\ud800\n" * 100_000,
            "### x\n" * 600_000,
            "````\n```\n" * 300_000,
            "```\n" + "### x\n" * 300_000,
            "".join(("`" * (3 + i % 5)) + "\n" for i in range(300_000)),
        ]
        with hang_guard(20):
            started = time.monotonic()
            for text in hostile:
                question_headings(text)
                mismatch_titles(text)
            assert time.monotonic() - started < 15

    def test_단독_CR도_줄바꿈이다(self) -> None:
        text = "### a\r- 종류: 어긋남\r### b\r- 종류: 미정\r"
        assert question_headings(text) == ["a", "b"]
        assert mismatch_titles(text) == ["a"]

    def test_빈_제목은_항목이_아니다(self) -> None:
        text = "### \n- 종류: 어긋남\n###  \t \n- 종류: 어긋남\n### 진짜\n- 종류: 어긋남\n"
        assert question_headings(text) == ["진짜"]
        assert mismatch_titles(text) == ["진짜"]

    def test_빈_문자열과_개행만(self) -> None:
        assert question_headings("") == []
        assert mismatch_titles("\n\n\r\n") == []


class TestTrack:
    def test_지난_스냅샷과_비교해_신규_해소를_기록한다(self, tmp_dir: Path) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        _write_oq(data, _oq(_item("[2026-10-06] a", "어긋남")))
        t1 = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
        at1 = "2026-10-07T09:00:00+09:00"
        assert track_open_questions(data, state, t1) == [
            OqEventRecord(at=at1, kind="opened", title="[2026-10-06] a")
        ]
        _write_oq(data, _oq(_item("[2026-10-08] b", "어긋남")))
        t2 = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
        at2 = "2026-10-08T09:00:00+09:00"
        assert track_open_questions(data, state, t2) == [
            OqEventRecord(at=at2, kind="opened", title="[2026-10-08] b"),
            OqEventRecord(at=at2, kind="resolved", title="[2026-10-06] a"),
        ]
        assert len(read_jsonl(state / STATE_FILES.oq_events, OqEventRecord)) == 3
        assert track_open_questions(data, state, t2) == []

    def test_파일이_없으면_빈_목록으로_본다(self, tmp_dir: Path) -> None:
        assert track_open_questions(tmp_dir / "d", tmp_dir / "s", datetime.now(UTC)) == []

    def test_같은_제목이_여럿이어도_한_번만_기록한다(self, tmp_dir: Path) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        _write_oq(data, _oq(_item("a", "어긋남") * 3))
        events = track_open_questions(data, state, datetime(2026, 10, 7, tzinfo=UTC))
        assert [e.title for e in events] == ["a"]

    def test_스냅샷이_쓰레기여도_예외가_없다(self, tmp_dir: Path) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        _write_oq(data, _oq(_item("a", "어긋남")))
        now = datetime(2026, 10, 7, tzinfo=UTC)
        garbage: list[bytes] = [
            b"\xff\xfe\x00{{{",
            b"[1, 2, 3]",
            b'{"titles": [1, 2]}',
            b'{"titles": "a"}',
            b'{"titles": ' + b"[1," * 100_000 + b"1]}",
            b"x" * 2_000_000,
            b"",
        ]
        for blob in garbage:
            state.mkdir(parents=True, exist_ok=True)
            (state / STATE_FILES.oq_snapshot).write_bytes(blob)
            events = track_open_questions(data, state, now)
            assert [(e.kind, e.title) for e in events] == [("opened", "a")]

    def test_스냅샷이_디렉터리나_링크여도_예외가_없다(self, tmp_dir: Path) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        _write_oq(data, _oq(_item("a", "어긋남")))
        state.mkdir()
        outside = tmp_dir / "outside.json"
        outside.write_text('{"titles": ["a"]}', encoding="utf-8")
        (state / STATE_FILES.oq_snapshot).symlink_to(outside)
        events = track_open_questions(data, state, datetime(2026, 10, 7, tzinfo=UTC))
        assert [(e.kind, e.title) for e in events] in ([], [("opened", "a")])

    def test_스냅샷의_거대한_제목은_다듬어_기록한다(self, tmp_dir: Path) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        state.mkdir()
        _write_oq(data, _oq(""))
        huge = "x" * 200_000 + "\x1b"
        (state / STATE_FILES.oq_snapshot).write_text(f'{{"titles": ["{huge[:-1]}", "a\\u001b"]}}')
        events = track_open_questions(data, state, datetime(2026, 10, 7, tzinfo=UTC))
        assert [e.kind for e in events] == ["resolved", "resolved"]
        assert len(events[0].title) == MAX_TITLE_CHARS
        assert events[1].title == "a\\x1b"

    def test_미결_파일이_링크_디렉터리_파이프여도_빈_것으로_본다(self, tmp_dir: Path) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        wiki = data / "wiki"
        wiki.mkdir(parents=True)
        real = tmp_dir / "real.md"
        real.write_text(_oq(_item("a", "어긋남")), encoding="utf-8")
        target = wiki / "open-questions.md"
        with hang_guard(10):
            target.symlink_to(real)
            assert track_open_questions(data, state, datetime(2026, 10, 7, tzinfo=UTC)) == []
            target.unlink()
            target.mkdir()
            assert track_open_questions(data, state, datetime(2026, 10, 7, tzinfo=UTC)) == []
            target.rmdir()
            os.mkfifo(target)
            assert track_open_questions(data, state, datetime(2026, 10, 7, tzinfo=UTC)) == []

    def test_스냅샷과_이벤트는_KST로_쓴다(self, tmp_dir: Path) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        _write_oq(data, _oq(_item("a", "어긋남")))
        track_open_questions(data, state, datetime(2026, 10, 7, 15, 0))  # naive는 UTC
        [event] = read_jsonl(state / STATE_FILES.oq_events, OqEventRecord)
        assert event.at == "2026-10-08T00:00:00+09:00"


_BOUNDARY_TITLES = [
    "a" * 299 + " b",
    "a" * 300 + " b",
    "a" * 298 + " " + "b" * 5,
    "가" * 299 + " 나",
    "a" * 298 + "\x1b" + "zz",
    "a" * 299 + "\x1b",
    "a" * 297 + "\u202e\u202e",
    "😀" * 299 + " x",
    "a" * 299 + "\u3000b",
    "a" * 298 + "\t\tb",
    " " * 5 + "a" * 295 + " " * 10 + "b",
]


class TestTitleStability:
    @pytest.mark.parametrize("title", _BOUNDARY_TITLES)
    def test_제목_정규화는_멱등이다(self, title: str) -> None:
        once = _title(title)
        assert _title(once) == once
        assert len(once) <= MAX_TITLE_CHARS
        assert once == once.strip()

    @pytest.mark.parametrize("title", _BOUNDARY_TITLES)
    def test_300자_경계_제목은_첫_실행에만_사건을_만든다(self, tmp_dir: Path, title: str) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        _write_oq(data, _oq(_item(title, "어긋남")))
        now = datetime(2026, 10, 7, tzinfo=UTC)
        first = track_open_questions(data, state, now)
        assert [e.kind for e in first] == ["opened"]
        assert track_open_questions(data, state, now) == []
        assert track_open_questions(data, state, now) == []
        assert len(read_jsonl(state / STATE_FILES.oq_events, OqEventRecord)) == 1

    def test_저장된_스냅샷이_한도_안에서_다시_읽힌다(self, tmp_dir: Path) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        items = "".join(_item(f"{i:04d}" + "😀" * 296, "어긋남") for i in range(MAX_OQ_ITEMS))
        _write_oq(data, _oq(items))
        now = datetime(2026, 10, 7, tzinfo=UTC)
        first = track_open_questions(data, state, now)
        assert 0 < len(first) < MAX_OQ_ITEMS  # 스냅샷에 들어가는 만큼만 추적한다
        assert (state / STATE_FILES.oq_snapshot).stat().st_size <= MAX_STATE_JSON
        assert all(e.kind == "opened" for e in first)
        assert track_open_questions(data, state, now) == []
        assert track_open_questions(data, state, now) == []

    def test_작은_스냅샷은_모두_추적한다(self, tmp_dir: Path) -> None:
        data, state = tmp_dir / "data", tmp_dir / "state"
        items = "".join(_item(f"{i:04d}" + "가" * 290, "어긋남") for i in range(MAX_OQ_ITEMS))
        _write_oq(data, _oq(items))
        now = datetime(2026, 10, 7, tzinfo=UTC)
        assert len(track_open_questions(data, state, now)) == MAX_OQ_ITEMS
        assert track_open_questions(data, state, now) == []
