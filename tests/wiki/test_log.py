from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from team_wiki_engine.wiki.frontmatter import parse_frontmatter
from team_wiki_engine.wiki.log import LogEntry, append_log, format_log_line


def _entry(
    at: datetime,
    kind: Literal["code", "ingest"] = "code",
    targets: str = "w #1",
    pages: list[str] | None = None,
    cost: float = 0.1,
) -> LogEntry:
    return LogEntry(
        at=at,
        kind=kind,
        targets=targets,
        pages=["status/w.md"] if pages is None else pages,
        cost_usd=cost,
    )


def _read_log(data_dir: Path) -> str:
    return (data_dir / "wiki" / "log.md").read_text(encoding="utf-8")


def _lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.startswith("- ")]


class TestFormatLogLine:
    def test_spec_format(self) -> None:
        line = format_log_line(
            LogEntry(
                at=datetime(2026, 10, 7, 0, 0, tzinfo=UTC),
                kind="code",
                targets="worker #14 #15, server #41",
                pages=["status/worker.md", "contracts/relay--worker--kafka.md"],
                cost_usd=0.384,
            )
        )
        assert line == (
            "- 2026-10-07 09:00 | code | worker #14 #15, server #41 "
            "→ status/worker.md, contracts/relay--worker--kafka.md | $0.38"
        )

    def test_no_changed_pages(self) -> None:
        line = format_log_line(
            LogEntry(
                at=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
                kind="ingest",
                targets="a.md",
                pages=[],
                cost_usd=0,
            )
        )
        assert line == "- 2026-10-07 21:00 | ingest | a.md → (변경 없음) | $0.00"

    def test_naive_datetime_is_utc(self) -> None:
        line = format_log_line(_entry(datetime(2026, 10, 7, 0, 0)))
        assert line.startswith("- 2026-10-07 09:00 | ")

    def test_other_timezone_is_converted_to_kst(self) -> None:
        pst = timezone(timedelta(hours=-8))
        line = format_log_line(_entry(datetime(2026, 10, 6, 10, 0, tzinfo=pst)))
        assert line.startswith("- 2026-10-07 03:00 | ")

    def test_date_rolls_over_in_kst(self) -> None:
        line = format_log_line(_entry(datetime(2026, 10, 7, 16, 30, tzinfo=UTC)))
        assert line.startswith("- 2026-10-08 01:30 | ")

    def test_cost_has_two_decimals(self) -> None:
        at = datetime(2026, 10, 7, tzinfo=UTC)
        assert format_log_line(_entry(at, cost=12)).endswith("| $12.00")
        assert format_log_line(_entry(at, cost=1.239)).endswith("| $1.24")


class TestFormatLogLineSanitizing:
    AT = datetime(2026, 10, 7, tzinfo=UTC)

    def test_control_characters_in_targets_become_spaces(self) -> None:
        samples = [
            "\r", "\n", "\t", "\x0b", "\x0c", "\x00", "\x1b", "\x1c", "\x1d", "\x1e", "\x1f",
            "\x7f", "\u2028", "\u2029", *(chr(code) for code in range(0x80, 0xA0)),
        ]  # fmt: skip
        for bad in samples:
            line = format_log_line(_entry(self.AT, targets=f"a{bad}b"))
            assert "a b" in line, repr(bad)
            assert "\n" not in line
            assert "\r" not in line

    def test_forged_log_line_and_frontmatter_fence_cannot_be_injected(self) -> None:
        line = format_log_line(
            _entry(
                self.AT,
                targets="x\n---\n- 2000-01-01 00:00 | code | fake → y | $0",
                pages=["p\n---"],
            )
        )
        assert len(line.splitlines()) == 1
        assert line.startswith("- 2026-10-07 09:00 | code | x --- - 2000-01-01")

    def test_pages_are_sanitized_individually(self) -> None:
        line = format_log_line(_entry(self.AT, pages=["a\nb.md", "c\u2028d.md"]))
        assert "→ a b.md, c d.md |" in line

    def test_run_of_controls_collapses_to_one_space(self) -> None:
        assert "a b" in format_log_line(_entry(self.AT, targets="a\r\n\tb"))

    def test_negative_cost_is_clamped_to_zero(self) -> None:
        for cost in (-0.001, -5, -0.0):
            assert format_log_line(_entry(self.AT, cost=cost)).endswith("| $0.00")

    def test_non_finite_cost_is_zero(self) -> None:
        for cost in (float("nan"), float("inf"), float("-inf")):
            assert format_log_line(_entry(self.AT, cost=cost)).endswith("| $0.00")


class TestAppendLogWithHostileFrontmatter:
    def test_malformed_scalar_in_existing_frontmatter_keeps_content(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        old = "---\na: !!int abc\n---\n\n# 옛 이력\n- 2026-01-01 09:00 | code | x → y | $0.01\n"
        log.write_text(old, encoding="utf-8")
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        parsed = parse_frontmatter(_read_log(tmp_dir))
        assert parsed.data == {"updated": "2026-10-07"}
        assert old in parsed.body
        assert len(_lines(parsed.body)) == 2


class TestUpdatedComparison:
    def _log_with_updated(self, tmp_dir: Path, value: str) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"---\nupdated: {value}\n---\n\n# 반영 이력\n\n", encoding="utf-8")

    def _updated_after_append(self, tmp_dir: Path) -> object:
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        data = parse_frontmatter(_read_log(tmp_dir)).data
        assert data is not None
        return data["updated"]

    def test_invalid_iso_dates_are_not_kept_as_later(self, tmp_dir: Path) -> None:
        for bad in ("9999-99-99", "2027-02-30", "٢٠٢٧-٠١-٠١", "2027-01-01garbage", "garbage"):
            self._log_with_updated(tmp_dir, f'"{bad}"')
            assert self._updated_after_append(tmp_dir) == "2026-10-07", bad

    def test_datetime_string_compares_by_its_date_part(self, tmp_dir: Path) -> None:
        self._log_with_updated(tmp_dir, "2027-01-01T09:00:00+09:00")
        assert self._updated_after_append(tmp_dir) == "2027-01-01T09:00:00+09:00"
        self._log_with_updated(tmp_dir, "2026-01-01T09:00:00+09:00")
        assert self._updated_after_append(tmp_dir) == "2026-10-07"

    def test_same_date_is_replaced_by_the_plain_date(self, tmp_dir: Path) -> None:
        self._log_with_updated(tmp_dir, "2026-10-07T01:00:00+09:00")
        assert self._updated_after_append(tmp_dir) == "2026-10-07"


class TestAppendLog:
    def test_creates_file_then_appends_and_updates_date(self, tmp_dir: Path) -> None:
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        append_log(
            tmp_dir,
            _entry(
                datetime(2026, 10, 8, tzinfo=UTC),
                kind="ingest",
                targets="a.md",
                pages=[],
                cost=0.2,
            ),
        )
        parsed = parse_frontmatter(_read_log(tmp_dir))
        assert parsed.data == {"updated": "2026-10-08"}
        assert _lines(parsed.body) == [
            "- 2026-10-07 09:00 | code | w #1 → status/w.md | $0.10",
            "- 2026-10-08 09:00 | ingest | a.md → (변경 없음) | $0.20",
        ]
        assert "# 반영 이력" in parsed.body

    def test_new_file_layout(self, tmp_dir: Path) -> None:
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        assert _read_log(tmp_dir) == (
            "---\nupdated: 2026-10-07\n---\n\n# 반영 이력\n\n"
            "- 2026-10-07 09:00 | code | w #1 → status/w.md | $0.10\n"
        )

    def test_older_entry_does_not_move_updated_back(self, tmp_dir: Path) -> None:
        append_log(tmp_dir, _entry(datetime(2026, 10, 8, tzinfo=UTC)))
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        text = _read_log(tmp_dir)
        assert parse_frontmatter(text).data == {"updated": "2026-10-08"}
        assert len(_lines(text)) == 2

    def test_existing_later_updated_is_kept(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        log.write_text("---\nupdated: 2027-01-01\n---\n\n# 반영 이력\n\n", encoding="utf-8")
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        assert parse_frontmatter(_read_log(tmp_dir)).data == {"updated": "2027-01-01"}

    def test_existing_earlier_updated_is_replaced(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        log.write_text("---\nupdated: 2026-01-01\n---\n\n# 반영 이력\n\n", encoding="utf-8")
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        assert parse_frontmatter(_read_log(tmp_dir)).data == {"updated": "2026-10-07"}

    def test_invalid_existing_updated_is_replaced(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        for bad in ('""', "tomorrow", "123", "[a]", "null"):
            log.write_text(f"---\nupdated: {bad}\n---\n\n# 반영 이력\n\n", encoding="utf-8")
            append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
            assert parse_frontmatter(_read_log(tmp_dir)).data == {"updated": "2026-10-07"}, bad

    def test_other_frontmatter_keys_are_kept_in_place(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        log.write_text(
            "---\nstatus: 확정\nupdated: 2026-01-01\nowners: [on]\n---\n\n# 반영 이력\n",
            encoding="utf-8",
        )
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        data = parse_frontmatter(_read_log(tmp_dir)).data
        assert data == {"status": "확정", "updated": "2026-10-07", "owners": ["on"]}
        assert data is not None
        assert list(data) == ["status", "updated", "owners"]

    def test_existing_file_without_frontmatter_is_preserved(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        old = "# 옛 이력\n\n- 2026-01-01 09:00 | code | x → y | $0.01\n"
        log.write_text(old, encoding="utf-8")
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        parsed = parse_frontmatter(_read_log(tmp_dir))
        assert parsed.data == {"updated": "2026-10-07"}
        assert old in parsed.body
        assert _lines(parsed.body) == [
            "- 2026-01-01 09:00 | code | x → y | $0.01",
            "- 2026-10-07 09:00 | code | w #1 → status/w.md | $0.10",
        ]

    def test_broken_frontmatter_content_is_preserved(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        old = (
            "---\nkey: [닫히지 않음\n---\n\n# 옛 이력\n\n"
            "- 2026-01-01 09:00 | code | x → y | $0.01\n"
        )
        log.write_text(old, encoding="utf-8")
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        text = _read_log(tmp_dir)
        parsed = parse_frontmatter(text)
        assert parsed.error is None
        assert parsed.data == {"updated": "2026-10-07"}
        assert old in parsed.body
        assert len(_lines(text)) == 2
        # 다시 덧붙여도 새 frontmatter만 갱신되고 내용은 그대로다
        append_log(tmp_dir, _entry(datetime(2026, 10, 9, tzinfo=UTC)))
        again = parse_frontmatter(_read_log(tmp_dir))
        assert again.data == {"updated": "2026-10-09"}
        assert old in again.body
        assert len(_lines(again.body)) == 3

    def test_non_mapping_frontmatter_content_is_preserved(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        old = "---\n- a\n- b\n---\n본문\n"
        log.write_text(old, encoding="utf-8")
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        assert old in parse_frontmatter(_read_log(tmp_dir)).body

    def test_existing_body_without_trailing_newline(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        log.write_text("---\nupdated: 2026-01-01\n---\n\n# 반영 이력", encoding="utf-8")
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        assert _read_log(tmp_dir).endswith(
            "# 반영 이력\n- 2026-10-07 09:00 | code | w #1 → status/w.md | $0.10\n"
        )

    def test_bom_in_existing_file_does_not_leak_into_body(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        log.write_bytes("\ufeff# 옛 이력\n".encode())
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        assert "\ufeff" not in _read_log(tmp_dir)

    def test_leaves_no_temp_files(self, tmp_dir: Path) -> None:
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        assert [p.name for p in (tmp_dir / "wiki").iterdir()] == ["log.md"]


class TestAppendLogDoesNotFollowLinks:
    def test_symlinked_log_is_refused_and_target_stays_unread_and_unchanged(
        self, tmp_dir: Path
    ) -> None:
        import os

        import pytest

        outside = tmp_dir / "outside.md"
        outside.write_text("SECRET-TARGET-TEXT\n", encoding="utf-8")
        log = tmp_dir / "data" / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        os.symlink(outside, log)
        with pytest.raises(OSError):
            append_log(tmp_dir / "data", _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        assert outside.read_text(encoding="utf-8") == "SECRET-TARGET-TEXT\n"
        assert log.is_symlink()

    def test_fifo_log_is_refused_without_hanging(self, tmp_dir: Path) -> None:
        import os

        import pytest

        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        os.mkfifo(log)
        with pytest.raises(OSError):
            append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))

    def test_regular_log_with_bom_still_works(self, tmp_dir: Path) -> None:
        log = tmp_dir / "wiki" / "log.md"
        log.parent.mkdir(parents=True)
        log.write_bytes("﻿---\nupdated: 2026-01-01\n---\n\n# 이력\n".encode())
        append_log(tmp_dir, _entry(datetime(2026, 10, 7, tzinfo=UTC)))
        assert "# 이력" in _read_log(tmp_dir)
        assert len(_lines(_read_log(tmp_dir))) == 1
