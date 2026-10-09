import dataclasses
import json
import os
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from team_wiki_engine.metrics.record import (
    MAX_JSONL_FILE,
    MAX_JSONL_LINE,
    MAX_STATE_JSON,
    STATE_FILES,
    GapConsumedRecord,
    GapRecord,
    OqEventRecord,
    PrReviewRecord,
    QuestionRecord,
    StateFiles,
    SyncRunRecord,
    append_jsonl,
    mark_gaps_consumed,
    read_json,
    read_jsonl,
    read_jsonl_window,
    unconsumed_gaps,
    write_json,
)
from team_wiki_engine.util.time import kst_iso
from tests.helpers.adapters import GAP_LIST
from tests.helpers.hang_guard import hang_guard, open_fd_count

AT = "2026-10-07T09:00:00+09:00"
BOM = b"\xef\xbb\xbf"


def _question(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "at": AT,
        "user": "u1",
        "thread_id": "t1",
        "ok": True,
        "failure": None,
        "source": "wiki",
        "duration_ms": 10,
        "cost_usd": 0.5,
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
        "resumed": False,
    }
    base.update(over)
    return base


def _gap(gap_id: str) -> GapRecord:
    return GapRecord(
        id=gap_id, at=AT, question=f"q-{gap_id}", answer_summary="a", code_paths=["x.kt"]
    )


class _N(BaseModel):
    n: int


class TestStateFiles:
    def test_names(self) -> None:
        assert dataclasses.asdict(STATE_FILES) == {
            "questions": "questions.jsonl",
            "gaps": "gaps.jsonl",
            "gaps_consumed": "gaps-consumed.jsonl",
            "sync_runs": "sync-runs.jsonl",
            "pr_review": "pr-review.jsonl",
            "oq_events": "oq-events.jsonl",
            "pr_tracking": "pr-tracking.json",
            "oq_snapshot": "oq-snapshot.json",
            "open_pr_snapshot": "open-prs-snapshot.json",
            "code_snapshot": "code-snapshot.json",
            "sessions": "sessions.json",
            "lock": "sync.lock",
            "data_lock": "data.lock",
            "bot_control": "bot-control.json",
        }
        assert isinstance(STATE_FILES, StateFiles)

    def test_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            STATE_FILES.questions = "x"  # type: ignore[misc]

    def test_limits(self) -> None:
        assert MAX_JSONL_LINE == 65_536
        assert MAX_STATE_JSON == 1_048_576
        assert MAX_JSONL_FILE == 8_388_608


class TestRecordModels:
    def test_question_valid_and_frozen(self) -> None:
        record = QuestionRecord.model_validate(_question())
        assert record.thread_id == "t1"
        with pytest.raises(Exception, match="frozen"):
            record.user = "x"

    def test_question_not_found_defaults_false_for_old_lines(self) -> None:
        old = _question()
        assert "not_found" not in old
        assert QuestionRecord.model_validate(old).not_found is False
        assert QuestionRecord.model_validate(_question(not_found=True)).not_found is True
        with pytest.raises(ValueError):
            QuestionRecord.model_validate(_question(not_found="true"))

    def test_question_source_may_be_null_and_code(self) -> None:
        assert QuestionRecord.model_validate(_question(source=None)).source is None
        assert QuestionRecord.model_validate(_question(source="code")).source == "code"
        with pytest.raises(ValueError):
            QuestionRecord.model_validate(_question(source="web"))

    def test_strict_rejects_coercion(self) -> None:
        for field, value in [("ok", "true"), ("duration_ms", "3"), ("input_tokens", 1.5)]:
            with pytest.raises(ValueError):
                QuestionRecord.model_validate(_question(**{field: value}))

    def test_bool_is_not_an_int(self) -> None:
        with pytest.raises(ValueError):
            QuestionRecord.model_validate(_question(duration_ms=True))

    @pytest.mark.parametrize(
        "field", ["duration_ms", "input_tokens", "output_tokens", "cache_read_input_tokens"]
    )
    def test_negative_integers_rejected(self, field: str) -> None:
        with pytest.raises(ValueError):
            QuestionRecord.model_validate(_question(**{field: -1}))

    def test_integer_upper_bound_is_pinned(self) -> None:
        ok = json.dumps(_question(duration_ms=2**63 - 1))
        assert QuestionRecord.model_validate_json(ok).duration_ms == 2**63 - 1
        with pytest.raises(ValueError):
            QuestionRecord.model_validate_json(json.dumps(_question(duration_ms=2**63)))
        with pytest.raises(ValueError):
            QuestionRecord.model_validate_json(json.dumps(_question(input_tokens=2**63)))

    def test_negative_cost_rejected(self) -> None:
        with pytest.raises(ValueError):
            QuestionRecord.model_validate(_question(cost_usd=-0.01))

    def test_integer_cost_accepted_in_json(self) -> None:
        line = json.dumps(_question(cost_usd=2))
        assert QuestionRecord.model_validate_json(line).cost_usd == 2.0

    def test_nan_and_inf_rejected_in_json(self) -> None:
        for token in ("NaN", "Infinity", "-Infinity"):
            base = json.dumps(_question(cost_usd=0))
            line = base.replace('"cost_usd": 0', f'"cost_usd": {token}')
            with pytest.raises(ValueError):
                QuestionRecord.model_validate_json(line)

    def test_unknown_keys_ignored(self) -> None:
        record = QuestionRecord.model_validate(_question(extra_key=1))
        assert "extra_key" not in record.model_dump()

    def test_other_models_roundtrip(self) -> None:
        records: list[BaseModel] = [
            GapConsumedRecord(id="g1", run_id="20261007-0900", at=AT),
            SyncRunRecord(
                at=AT,
                run_id="r",
                kind="code",
                result="pr",
                pr="12",
                lint_failed=False,
                cost_usd=0.1,
                input_tokens=1,
                output_tokens=2,
                duration_ms=3,
            ),
            PrReviewRecord(
                at=AT,
                pr="12",
                kind="ingest",
                outcome="merged",
                human_changed_files=None,
                human_additions=3,
                human_deletions=0,
                closed_at=None,
            ),
            OqEventRecord(at=AT, kind="opened", title="미결"),
            _gap("g1"),
        ]
        for record in records:
            assert type(record).model_validate_json(record.model_dump_json()) == record

    def test_pr_review_outcome_and_counts_validated(self) -> None:
        good = {
            "at": AT,
            "pr": "1",
            "kind": "code",
            "outcome": "merged",
            "human_changed_files": 0,
            "human_additions": 0,
            "human_deletions": 0,
            "closed_at": None,
        }
        assert PrReviewRecord.model_validate(good).outcome == "merged"
        for bad in ({"outcome": "open"}, {"human_additions": -1}, {"pr": 12}):
            with pytest.raises(ValueError):
                PrReviewRecord.model_validate({**good, **bad})


class TestJsonl:
    def test_append_and_read_back(self, tmp_dir: Path) -> None:
        file = tmp_dir / "a" / "b.jsonl"
        append_jsonl(file, _N(n=1))
        append_jsonl(file, _N(n=2))
        assert read_jsonl(file, _N) == [_N(n=1), _N(n=2)]
        assert file.read_bytes() == b'{"n":1}\n{"n":2}\n'

    def test_non_ascii_written_as_utf8(self, tmp_dir: Path) -> None:
        file = tmp_dir / "o.jsonl"
        append_jsonl(file, OqEventRecord(at=AT, kind="opened", title="한글 제목"))
        assert "한글 제목".encode() in file.read_bytes()
        assert read_jsonl(file, OqEventRecord)[0].title == "한글 제목"

    def test_broken_lines_skipped_missing_file_empty(self, tmp_dir: Path) -> None:
        file = tmp_dir / "x.jsonl"
        file.write_text('{"n":1}\n{깨짐\n\n   \n{"n":3}\n', encoding="utf-8")
        assert read_jsonl(file, _N) == [_N(n=1), _N(n=3)]
        assert read_jsonl(tmp_dir / "none.jsonl", _N) == []

    def test_last_line_without_newline_and_crlf(self, tmp_dir: Path) -> None:
        file = tmp_dir / "x.jsonl"
        file.write_bytes(b'{"n":1}\r\n{"n":2}')
        assert read_jsonl(file, _N) == [_N(n=1), _N(n=2)]

    def test_directory_is_empty_list(self, tmp_dir: Path) -> None:
        assert read_jsonl(tmp_dir, _N) == []

    def test_unreadable_file_is_empty_list(self, tmp_dir: Path) -> None:
        file = tmp_dir / "x.jsonl"
        file.write_text('{"n":1}\n', encoding="utf-8")
        file.chmod(0)
        try:
            if os.access(file, os.R_OK):
                pytest.skip("권한이 있는 사용자(root 등)라 읽기가 막히지 않는다")
            assert read_jsonl(file, _N) == []
        finally:
            file.chmod(0o600)

    def test_hostile_lines_dropped_good_lines_kept(self, tmp_dir: Path) -> None:
        good_before = json.dumps(_question(user="before"))
        good_after = json.dumps(_question(user="after"))
        base = json.dumps(_question())
        bad_lines: list[bytes] = [
            base.replace('"cost_usd": 0.5', '"cost_usd": NaN').encode(),
            base.replace('"cost_usd": 0.5', '"cost_usd": Infinity').encode(),
            base.replace('"cost_usd": 0.5', f'"cost_usd": {"9" * 5000}').encode(),
            base.replace('"duration_ms": 10', f'"duration_ms": {"9" * 5000}').encode(),
            b"[" * 50_000,  # 줄 상한 안의 깊은 중첩
            b"[" * 100_000,  # 줄 상한 밖
            base.encode().replace(b'"u1"', b'"\xff\xfe"'),  # 잘못된 UTF-8
            b'{"user": "' + b"x" * 200_000 + b'"}',  # 200KB 한 줄
            base.replace('"ok": true', '"ok": "true"').encode(),
            b"null",
            b"123",
            b"[]",
            b"\x00\x00\x00",
        ]
        file = tmp_dir / "q.jsonl"
        data = good_before.encode() + b"\n"
        for bad in bad_lines:
            data += bad + b"\n"
        data += json.dumps(_question(user="extra", brand_new_key=[1, 2])).encode() + b"\n"
        data += good_after.encode() + b"\n"
        file.write_bytes(data)
        records = read_jsonl(file, QuestionRecord)
        assert [r.user for r in records] == ["before", "extra", "after"]

    def test_long_line_at_end_of_file_without_newline(self, tmp_dir: Path) -> None:
        file = tmp_dir / "q.jsonl"
        file.write_bytes(b'{"n":1}\n' + b"x" * (3 * MAX_JSONL_LINE))
        assert read_jsonl(file, _N) == [_N(n=1)]

    def test_line_exactly_at_limit_is_read_and_one_over_is_dropped(self, tmp_dir: Path) -> None:
        def padded(total: int) -> bytes:
            head = b'{"n":1,"pad":"'
            tail = b'"}'
            return head + b"p" * (total - len(head) - len(tail)) + tail

        file = tmp_dir / "q.jsonl"
        file.write_bytes(
            padded(MAX_JSONL_LINE) + b"\n" + padded(MAX_JSONL_LINE + 1) + b'\n{"n":2}\n'
        )
        assert read_jsonl(file, _N) == [_N(n=1), _N(n=2)]

    def test_append_creates_file_with_mode_600(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sub" / "m.jsonl"
        append_jsonl(file, _N(n=1))
        assert stat.S_IMODE(os.stat(file).st_mode) == 0o600

    def test_append_to_symlink_raises_oserror_and_leaves_target(self, tmp_dir: Path) -> None:
        target = tmp_dir / "target.jsonl"
        target.write_text("원본\n", encoding="utf-8")
        link = tmp_dir / "link.jsonl"
        link.symlink_to(target)
        with pytest.raises(OSError):
            append_jsonl(link, _N(n=1))
        assert target.read_text(encoding="utf-8") == "원본\n"
        assert link.is_symlink()

    def test_append_to_broken_symlink_does_not_create_target(self, tmp_dir: Path) -> None:
        target = tmp_dir / "never.jsonl"
        link = tmp_dir / "link.jsonl"
        link.symlink_to(target)
        with pytest.raises(OSError):
            append_jsonl(link, _N(n=1))
        assert not target.exists()

    def test_oversized_record_raises_value_error_without_content(self, tmp_dir: Path) -> None:
        file = tmp_dir / "big.jsonl"
        secret = "SECRETMARKER-" * 20_000
        with pytest.raises(ValueError) as info:
            append_jsonl(file, OqEventRecord(at=AT, kind="opened", title=secret))
        message = str(info.value)
        assert "OqEventRecord" in message
        assert "SECRETMARKER" not in message
        assert len(message) < 200
        assert not file.exists()

    def test_record_exactly_at_limit_is_written(self, tmp_dir: Path) -> None:
        file = tmp_dir / "edge.jsonl"
        empty = len(OqEventRecord(at=AT, kind="opened", title="").model_dump_json().encode())
        title = "a" * (MAX_JSONL_LINE - empty)
        append_jsonl(file, OqEventRecord(at=AT, kind="opened", title=title))
        assert len(read_jsonl(file, OqEventRecord)) == 1
        with pytest.raises(ValueError):
            append_jsonl(file, OqEventRecord(at=AT, kind="opened", title=title + "a"))
        assert len(read_jsonl(file, OqEventRecord)) == 1

    def test_partial_writes_are_continued(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_write = os.write

        def short_write(fd: int, data: Any) -> int:
            return real_write(fd, bytes(data)[:3])

        monkeypatch.setattr(os, "write", short_write)
        file = tmp_dir / "p.jsonl"
        append_jsonl(file, _N(n=12345))
        monkeypatch.undo()
        assert file.read_bytes() == b'{"n":12345}\n'


class TestJsonlRobustness:
    def test_remainder_of_overlong_line_is_not_read_as_a_record(self, tmp_dir: Path) -> None:
        file = tmp_dir / "q.jsonl"
        # 첫 MAX+1바이트에서 읽기가 끊기고, 나머지(`{"n":99}`)는 그 자체로 올바른 기록이다.
        overlong = b"x" * (MAX_JSONL_LINE + 1) + b'{"n":99}'
        file.write_bytes(b'{"n":1}\n' + overlong + b'\n{"n":2}\n')
        assert read_jsonl(file, _N) == [_N(n=1), _N(n=2)]

    def test_remainder_spanning_several_chunks_is_discarded(self, tmp_dir: Path) -> None:
        file = tmp_dir / "q.jsonl"
        overlong = b"x" * (2 * MAX_JSONL_LINE + 5) + b'{"n":99}'
        file.write_bytes(overlong + b'\n{"n":2}\n')
        assert read_jsonl(file, _N) == [_N(n=2)]

    def test_bom_at_file_start_does_not_drop_first_record(self, tmp_dir: Path) -> None:
        file = tmp_dir / "q.jsonl"
        file.write_bytes(BOM + b'{"n":1}\n{"n":2}\n')
        assert read_jsonl(file, _N) == [_N(n=1), _N(n=2)]

    def test_stops_reading_after_total_size_cap(self, tmp_dir: Path) -> None:
        file = tmp_dir / "big.jsonl"
        junk = b"j" * 60_000 + b"\n"
        with file.open("wb") as handle:
            handle.write(b'{"n":1}\n')
            for _ in range(MAX_JSONL_FILE // len(junk) + 2):
                handle.write(junk)
            handle.write(b'{"n":2}\n')
        assert file.stat().st_size > MAX_JSONL_FILE
        assert read_jsonl(file, _N) == [_N(n=1)]

    def test_eight_mebibytes_of_junk_lines_is_cheap(self, tmp_dir: Path) -> None:
        file = tmp_dir / "junk.jsonl"
        file.write_bytes(b"x\n" * (MAX_JSONL_FILE // 2))
        assert file.stat().st_size == MAX_JSONL_FILE
        started = time.monotonic()
        with hang_guard(20):
            assert read_jsonl(file, _N) == []
        assert time.monotonic() - started < 2.0

    def test_junk_lines_are_not_sent_to_the_validator(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[bytes] = []
        real = _N.model_validate_json

        def spy(data: bytes | str, **kwargs: Any) -> _N:
            calls.append(bytes(data) if isinstance(data, (bytes, bytearray)) else data.encode())
            return real(data, **kwargs)

        monkeypatch.setattr(_N, "model_validate_json", spy)
        file = tmp_dir / "q.jsonl"
        file.write_bytes(b'x\n[1]\n  "s"\n123\n  {"n":1}\n\xff\n' + "{깨짐\n".encode())
        assert read_jsonl(file, _N) == [_N(n=1)]
        assert calls == [b'{"n":1}', "{깨짐".encode()]  # `{`로 시작하는 줄만 검증기까지 간다

    def test_records_after_the_cap_are_dropped(self, tmp_dir: Path) -> None:
        file = tmp_dir / "late.jsonl"
        with file.open("wb") as handle:
            handle.write(b'{"n":1}\n')
            junk = b"j" * 60_000 + b"\n"
            for _ in range(MAX_JSONL_FILE // len(junk) + 2):
                handle.write(junk)
            handle.write(b'{"n":2}\n')
        assert read_jsonl(file, _N) == [_N(n=1)]  # 상한 뒤의 최신 기록은 버려진다

    def test_fifo_with_no_writer_returns_empty_without_hanging(self, tmp_dir: Path) -> None:
        fifo = tmp_dir / "pipe.jsonl"
        os.mkfifo(fifo)
        with hang_guard():
            assert read_jsonl(fifo, _N) == []

    @pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="/dev/zero가 없다")
    def test_symlink_to_dev_zero_returns_empty_without_hanging(self, tmp_dir: Path) -> None:
        link = tmp_dir / "zero.jsonl"
        link.symlink_to("/dev/zero")
        with hang_guard():
            assert read_jsonl(link, _N) == []

    def test_does_not_leak_file_descriptors(self, tmp_dir: Path) -> None:
        (tmp_dir / "d").mkdir()
        os.mkfifo(tmp_dir / "pipe")
        good = tmp_dir / "g.jsonl"
        good.write_bytes(b'{"n":1}\n')
        targets = [tmp_dir / "d", tmp_dir / "pipe", tmp_dir / "none", good]
        for target in targets:
            read_jsonl(target, _N)
        before = open_fd_count()
        with hang_guard():
            for _ in range(200):
                for target in targets:
                    read_jsonl(target, _N)
        assert open_fd_count() == before

    def test_append_to_fifo_raises_oserror_without_hanging(self, tmp_dir: Path) -> None:
        fifo = tmp_dir / "pipe.jsonl"
        os.mkfifo(fifo)
        with hang_guard(), pytest.raises(OSError):  # 읽는 쪽이 없으면 열기가 ENXIO로 끝난다
            append_jsonl(fifo, _N(n=1))

    def test_append_to_fifo_with_reader_is_refused_by_kind_check(self, tmp_dir: Path) -> None:
        fifo = tmp_dir / "pipe.jsonl"
        os.mkfifo(fifo)
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with hang_guard(), pytest.raises(OSError):
                append_jsonl(fifo, _N(n=1))
            assert os.read(reader, 100) == b""  # 아무것도 쓰이지 않았다
        finally:
            os.close(reader)

    def test_append_to_directory_raises_oserror_without_leaking(self, tmp_dir: Path) -> None:
        (tmp_dir / "d").mkdir()
        with pytest.raises(OSError):
            append_jsonl(tmp_dir / "d", _N(n=1))
        before = open_fd_count()
        for _ in range(100):
            with pytest.raises(OSError):
                append_jsonl(tmp_dir / "d", _N(n=1))
        assert open_fd_count() == before

    def test_zero_byte_write_raises_oserror_instead_of_looping(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os, "write", lambda fd, data: 0)
        with hang_guard(), pytest.raises(OSError):
            append_jsonl(tmp_dir / "z.jsonl", _N(n=1))

    def test_append_does_not_leak_file_descriptors_on_success(self, tmp_dir: Path) -> None:
        file = tmp_dir / "a.jsonl"
        append_jsonl(file, _N(n=1))
        before = open_fd_count()
        for _ in range(100):
            append_jsonl(file, _N(n=1))
        assert open_fd_count() == before


class TestJson:
    def test_missing_or_broken_returns_fallback(self, tmp_dir: Path) -> None:
        adapter = TypeAdapter(dict[str, int])
        assert read_json(tmp_dir / "none.json", adapter, {"a": 1}) == {"a": 1}
        (tmp_dir / "bad.json").write_text("{", encoding="utf-8")
        assert read_json(tmp_dir / "bad.json", adapter, {}) == {}

    def test_garbage_bytes_and_wrong_shape(self, tmp_dir: Path) -> None:
        adapter = TypeAdapter(dict[str, int])
        file = tmp_dir / "g.json"
        file.write_bytes(b"\xff\xfe\x00garbage")
        assert read_json(file, adapter, {"d": 0}) == {"d": 0}
        file.write_text("[]", encoding="utf-8")
        assert read_json(file, adapter, {"d": 0}) == {"d": 0}
        file.write_text('{"a": "x"}', encoding="utf-8")
        assert read_json(file, adapter, {"d": 0}) == {"d": 0}

    def test_deeply_nested_and_huge_int_return_fallback(self, tmp_dir: Path) -> None:
        file = tmp_dir / "deep.json"
        file.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        assert read_json(file, TypeAdapter(list[Any]), ["fallback"]) == ["fallback"]
        file.write_text('{"a": ' + "9" * 5000 + "}", encoding="utf-8")
        assert read_json(file, TypeAdapter(dict[str, float]), {}) == {}

    def test_oversized_file_returns_fallback_even_if_valid(self, tmp_dir: Path) -> None:
        file = tmp_dir / "big.json"
        with file.open("wb") as handle:
            handle.write(b'{"a": 1}')
            handle.write(b" " * (MAX_STATE_JSON + 1 - 8))
        assert file.stat().st_size == MAX_STATE_JSON + 1
        assert read_json(file, TypeAdapter(dict[str, int]), {"d": 0}) == {"d": 0}

    def test_file_at_limit_is_read(self, tmp_dir: Path) -> None:
        file = tmp_dir / "edge.json"
        with file.open("wb") as handle:
            handle.write(b'{"a": 1}')
            handle.write(b" " * (MAX_STATE_JSON - 8))
        assert read_json(file, TypeAdapter(dict[str, int]), {}) == {"a": 1}

    def test_two_mebibyte_file_returns_fallback(self, tmp_dir: Path) -> None:
        file = tmp_dir / "two.json"
        file.write_bytes(b'{"a": 1}' + b" " * (2 * 1024 * 1024))
        assert read_json(file, TypeAdapter(dict[str, int]), {"d": 0}) == {"d": 0}

    def test_many_empty_objects_against_list_adapter_fails_fast(self, tmp_dir: Path) -> None:
        file = tmp_dir / "amp.json"
        data = b"[" + b"{}," * ((MAX_STATE_JSON - 4) // 3) + b"{}]"
        assert len(data) <= MAX_STATE_JSON
        file.write_bytes(data)
        started = time.monotonic()
        with hang_guard(10):
            assert read_json(file, GAP_LIST, []) == []
        assert time.monotonic() - started < 2.0

    def test_failfast_list_adapter_uses_far_less_memory_than_a_plain_one(
        self, tmp_dir: Path
    ) -> None:
        file = tmp_dir / "amp.json"
        file.write_bytes(b"[" + b"{}," * ((MAX_STATE_JSON - 4) // 3) + b"{}]")
        script = (
            "import resource, sys\n"
            "from pathlib import Path\n"
            "from pydantic import TypeAdapter\n"
            "from team_wiki_engine.metrics.record import GapRecord, read_json\n"
            "from tests.helpers.adapters import GAP_LIST\n"
            "adapter = GAP_LIST if sys.argv[1] == 'tested' else TypeAdapter(list[GapRecord])\n"
            "assert read_json(Path(sys.argv[2]), adapter, []) == []\n"
            "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)\n"
        )
        repo_root = Path(__file__).resolve().parents[2]

        def peak(which: str) -> int:
            done = subprocess.run(
                [sys.executable, "-c", script, which, str(file)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            return int(done.stdout.strip())

        tested, plain = peak("tested"), peak("plain")  # 단위는 같은 운영체제 안에서만 비교한다
        assert plain >= 3 * tested

    def test_bom_is_skipped(self, tmp_dir: Path) -> None:
        file = tmp_dir / "bom.json"
        file.write_bytes(BOM + b'{"a": 1}')
        assert read_json(file, TypeAdapter(dict[str, int]), {}) == {"a": 1}

    def test_fifo_with_no_writer_returns_fallback_without_hanging(self, tmp_dir: Path) -> None:
        fifo = tmp_dir / "pipe.json"
        os.mkfifo(fifo)
        with hang_guard():
            assert read_json(fifo, TypeAdapter(list[int]), [7]) == [7]

    @pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="/dev/zero가 없다")
    def test_symlink_to_dev_zero_returns_fallback_without_hanging(self, tmp_dir: Path) -> None:
        link = tmp_dir / "zero.json"
        link.symlink_to("/dev/zero")
        with hang_guard():
            assert read_json(link, TypeAdapter(list[int]), [7]) == [7]

    def test_does_not_leak_file_descriptors(self, tmp_dir: Path) -> None:
        (tmp_dir / "d").mkdir()
        os.mkfifo(tmp_dir / "pipe")
        good = tmp_dir / "g.json"
        good.write_text("[1]", encoding="utf-8")
        adapter = TypeAdapter(list[int])
        targets = [tmp_dir / "d", tmp_dir / "pipe", tmp_dir / "none", good]
        for target in targets:
            read_json(target, adapter, [])
        before = open_fd_count()
        with hang_guard():
            for _ in range(200):
                for target in targets:
                    read_json(target, adapter, [])
        assert open_fd_count() == before

    def test_returns_the_fallback_object_itself(self, tmp_dir: Path) -> None:
        fallback: list[int] = []
        assert read_json(tmp_dir / "none.json", TypeAdapter(list[int]), fallback) is fallback

    def test_non_finite_float_is_refused_without_content_in_message(self, tmp_dir: Path) -> None:
        file = tmp_dir / "nan.json"
        file.write_text("원본", encoding="utf-8")
        for bad in (float("nan"), float("inf")):
            with pytest.raises(ValueError) as info:
                write_json(file, {"SECRETMARKER": bad})
            assert "SECRETMARKER" not in str(info.value)
        assert file.read_text(encoding="utf-8") == "원본"

    def test_directory_returns_fallback(self, tmp_dir: Path) -> None:
        assert read_json(tmp_dir, TypeAdapter(list[int]), [7]) == [7]

    def test_write_and_read_back(self, tmp_dir: Path) -> None:
        file = tmp_dir / "c" / "d.json"
        write_json(file, {"b": [1], "한글": "값"})
        text = file.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert "한글" in text
        assert '  "b"' in text  # 들여쓰기 2칸
        assert read_json(file, TypeAdapter(dict[str, Any]), {}) == {"b": [1], "한글": "값"}

    def test_write_pydantic_models_and_read_back(self, tmp_dir: Path) -> None:
        file = tmp_dir / "gaps.json"
        gaps = [_gap("g1"), _gap("g2")]
        write_json(file, gaps)
        assert read_json(file, GAP_LIST, []) == gaps
        assert json.loads(file.read_text(encoding="utf-8"))[0]["answer_summary"] == "a"


class TestGaps:
    def test_unconsumed_in_file_order_with_limit(self, tmp_dir: Path) -> None:
        for gap_id in ("g1", "g2", "g3"):
            append_jsonl(tmp_dir / STATE_FILES.gaps, _gap(gap_id))
        at = datetime(2026, 10, 7, 0, 0, 0, tzinfo=UTC)
        mark_gaps_consumed(tmp_dir, ["g1"], "20261007-0900", at)
        assert [g.id for g in unconsumed_gaps(tmp_dir, 10)] == ["g2", "g3"]
        assert [g.id for g in unconsumed_gaps(tmp_dir, 1)] == ["g3"]
        consumed = (tmp_dir / STATE_FILES.gaps_consumed).read_text(encoding="utf-8")
        assert [json.loads(line) for line in consumed.splitlines()] == [
            {"id": "g1", "run_id": "20261007-0900", "at": "2026-10-07T09:00:00+09:00"}
        ]
        assert kst_iso(at) == "2026-10-07T09:00:00+09:00"

    @pytest.mark.parametrize("limit", [0, -1, -100])
    def test_non_positive_limit_is_empty(self, tmp_dir: Path, limit: int) -> None:
        append_jsonl(tmp_dir / STATE_FILES.gaps, _gap("g1"))
        assert unconsumed_gaps(tmp_dir, limit) == []

    @pytest.mark.parametrize(
        ("limit", "expected"),
        [
            (1, ["g5"]),
            (2, ["g4", "g5"]),
            (3, ["g3", "g4", "g5"]),
            (5, ["g1", "g2", "g3", "g4", "g5"]),
            (6, ["g1", "g2", "g3", "g4", "g5"]),
            (10**9, ["g1", "g2", "g3", "g4", "g5"]),
        ],
    )
    def test_mid_range_limits(self, tmp_dir: Path, limit: int, expected: list[str]) -> None:
        for gap_id in ("g1", "g2", "g3", "g4", "g5"):
            append_jsonl(tmp_dir / STATE_FILES.gaps, _gap(gap_id))
        assert [g.id for g in unconsumed_gaps(tmp_dir, limit)] == expected

    def test_limit_applies_after_removing_consumed(self, tmp_dir: Path) -> None:
        for gap_id in ("g1", "g2", "g3", "g4", "g5"):
            append_jsonl(tmp_dir / STATE_FILES.gaps, _gap(gap_id))
        mark_gaps_consumed(tmp_dir, ["g5"], "r", datetime(2026, 10, 7, tzinfo=UTC))
        assert [g.id for g in unconsumed_gaps(tmp_dir, 2)] == ["g3", "g4"]

    def test_duplicate_ids_in_gaps_file_keep_the_first(self, tmp_dir: Path) -> None:
        file = tmp_dir / STATE_FILES.gaps
        append_jsonl(file, _gap("g1"))
        append_jsonl(file, _gap("g2"))
        append_jsonl(
            file, GapRecord(id="g1", at=AT, question="늦은", answer_summary="", code_paths=[])
        )
        got = unconsumed_gaps(tmp_dir, 10)
        assert [g.id for g in got] == ["g1", "g2"]
        assert got[0].question == "q-g1"

    def test_mark_deduplicates_ids(self, tmp_dir: Path) -> None:
        mark_gaps_consumed(tmp_dir, ["g1", "g2", "g1"], "r", datetime(2026, 10, 7, tzinfo=UTC))
        lines = (tmp_dir / STATE_FILES.gaps_consumed).read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["id"] for line in lines] == ["g1", "g2"]

    def test_mark_rejects_a_bare_string_without_content_in_message(self, tmp_dir: Path) -> None:
        with pytest.raises(TypeError) as info:
            mark_gaps_consumed(tmp_dir, "SECRETMARKER", "r", datetime(2026, 10, 7, tzinfo=UTC))
        assert "SECRETMARKER" not in str(info.value)
        assert not (tmp_dir / STATE_FILES.gaps_consumed).exists()

    def test_no_files_is_empty(self, tmp_dir: Path) -> None:
        assert unconsumed_gaps(tmp_dir, 5) == []
        assert unconsumed_gaps(tmp_dir / "missing", 5) == []

    def test_all_consumed_and_broken_lines_ignored(self, tmp_dir: Path) -> None:
        append_jsonl(tmp_dir / STATE_FILES.gaps, _gap("g1"))
        with (tmp_dir / STATE_FILES.gaps).open("a", encoding="utf-8") as handle:
            handle.write("{깨짐\n")
        mark_gaps_consumed(tmp_dir, ["g1"], "r", datetime(2026, 10, 7, tzinfo=UTC))
        assert unconsumed_gaps(tmp_dir, 5) == []

    def test_mark_does_not_rewrite_gaps_file(self, tmp_dir: Path) -> None:
        append_jsonl(tmp_dir / STATE_FILES.gaps, _gap("g1"))
        before = (tmp_dir / STATE_FILES.gaps).read_bytes()
        mark_gaps_consumed(tmp_dir, ["g1"], "r", datetime(2026, 10, 7, tzinfo=UTC))
        assert (tmp_dir / STATE_FILES.gaps).read_bytes() == before
        mark_gaps_consumed(tmp_dir, [], "r", datetime(2026, 10, 7, tzinfo=UTC))


class _Line(BaseModel):
    n: int


def _fixed_line(i: int) -> bytes:
    body = b'{"n":%d,"pad":"' % i
    return body + b"p" * (63 - len(body) - 2) + b'"}\n'  # 줄바꿈 포함 정확히 64바이트


class TestTailRead:
    def test_작은_파일은_tail이어도_같다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "a.jsonl"
        file.write_bytes(b'{"n":1}\nx\n{"n":2}\n')
        assert read_jsonl(file, _N, tail=True) == read_jsonl(file, _N) == [_N(n=1), _N(n=2)]
        window = read_jsonl_window(file, _N)
        assert window.records == [_N(n=1), _N(n=2)]
        assert window.truncated is False

    def test_큰_파일은_끝_8MiB를_읽는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "big.jsonl"
        count = MAX_JSONL_FILE // 64 * 2
        file.write_bytes(b"".join(_fixed_line(i) for i in range(count)))
        assert file.stat().st_size == 2 * MAX_JSONL_FILE
        records = read_jsonl(file, _Line, tail=True)
        assert len(records) == MAX_JSONL_FILE // 64  # 줄 경계에 딱 맞으면 첫 줄을 버리지 않는다
        assert records[0].n == count - MAX_JSONL_FILE // 64
        assert records[-1].n == count - 1
        window = read_jsonl_window(file, _Line)
        assert window.truncated is True
        assert window.records == records

    def test_줄_중간에서_시작하면_잘린_첫_줄을_버린다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "mid.jsonl"
        count = MAX_JSONL_FILE // 64 + 100
        data = b"".join(_fixed_line(i) for i in range(count - 1)) + b'{"n":%d}\n' % (count - 1)
        file.write_bytes(data)  # 마지막 줄이 짧아 경계가 줄 중간이 된다
        assert (file.stat().st_size - MAX_JSONL_FILE) % 64 != 0
        records = read_jsonl(file, _Line, tail=True)
        ns = [r.n for r in records]
        assert ns == sorted(ns)
        assert ns[-1] == count - 1
        assert len(set(ns)) == len(ns)
        assert MAX_JSONL_FILE // 64 - 3 <= len(ns) <= MAX_JSONL_FILE // 64
        assert ns[0] == 100  # 99번 줄은 중간에서 잘려 버려지고 100번부터 읽는다
        assert ns[1] - ns[0] == 1

    def test_기본_읽기는_그대로_앞쪽을_읽는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "big.jsonl"
        file.write_bytes(b"".join(_fixed_line(i) for i in range(MAX_JSONL_FILE // 64 * 2)))
        head = read_jsonl(file, _Line)
        assert head[0].n == 0

    def test_적대적_파일도_예외_없이_빈_결과(self, tmp_dir: Path) -> None:
        with hang_guard():
            assert read_jsonl(tmp_dir / "없음", _N, tail=True) == []
            (tmp_dir / "dir").mkdir()
            assert read_jsonl(tmp_dir / "dir", _N, tail=True) == []
            os.mkfifo(tmp_dir / "pipe")
            assert read_jsonl(tmp_dir / "pipe", _N, tail=True) == []
            window = read_jsonl_window(tmp_dir / "pipe", _N)
            assert (window.records, window.truncated) == ([], False)

    def test_큰_파일의_끝이_쓰레기여도_예외가_없다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "junk.jsonl"
        file.write_bytes(b'{"n":1}\n' + b"\xff{x" * (MAX_JSONL_FILE // 2))
        with hang_guard(20):
            assert read_jsonl(file, _N, tail=True) == []

    def test_한_줄이_8MiB를_넘는_큰_파일도_끝난다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "long.jsonl"
        file.write_bytes(b"x" * (MAX_JSONL_FILE + 5000) + b'\n{"n":7}\n')
        with hang_guard(20):
            assert read_jsonl(file, _N, tail=True) == [_N(n=7)]
