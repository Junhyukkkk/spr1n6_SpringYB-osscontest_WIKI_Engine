import json
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.wiki.baseline import (
    Baseline,
    RepoBaseline,
    empty_baseline,
    read_baseline,
    write_baseline,
)

SHA = "a" * 40


def _write_raw(data_dir: Path, content: str | bytes) -> None:
    target = data_dir / "baseline.json"
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def _write_json(data_dir: Path, value: Any) -> None:
    _write_raw(data_dir, json.dumps(value))


class TestEmptyBaseline:
    def test_shape(self) -> None:
        assert empty_baseline() == Baseline(repos={}, raw_processed=[], updated_at=None)

    def test_each_call_returns_independent_object(self) -> None:
        first = empty_baseline()
        first.raw_processed.append("a.md")
        first.repos["w"] = RepoBaseline(commit=SHA, archived=False)
        assert empty_baseline() == Baseline(repos={}, raw_processed=[], updated_at=None)


class TestReadBaseline:
    def test_missing_file_is_empty(self, tmp_dir: Path) -> None:
        assert read_baseline(tmp_dir) == empty_baseline()

    def test_empty_object_fills_defaults(self, tmp_dir: Path) -> None:
        _write_json(tmp_dir, {})
        assert read_baseline(tmp_dir) == empty_baseline()

    def test_reads_full_document(self, tmp_dir: Path) -> None:
        _write_json(
            tmp_dir,
            {
                "repos": {"worker": {"commit": SHA, "archived": True}},
                "raw_processed": ["a.md"],
                "updated_at": "2026-10-07T09:00:00+09:00",
            },
        )
        assert read_baseline(tmp_dir) == Baseline(
            repos={"worker": RepoBaseline(commit=SHA, archived=True)},
            raw_processed=["a.md"],
            updated_at="2026-10-07T09:00:00+09:00",
        )

    def test_null_updated_at_is_allowed(self, tmp_dir: Path) -> None:
        _write_json(tmp_dir, {"updated_at": None})
        assert read_baseline(tmp_dir).updated_at is None

    def test_unknown_keys_are_ignored(self, tmp_dir: Path) -> None:
        _write_json(
            tmp_dir,
            {"extra": 1, "repos": {"w": {"commit": SHA, "archived": False, "note": "x"}}},
        )
        assert read_baseline(tmp_dir).repos == {"w": RepoBaseline(commit=SHA, archived=False)}

    @pytest.mark.parametrize(
        "commit",
        ["abc", "A" * 40, "a" * 39, "a" * 41, "g" * 40, SHA + "\n", " " + SHA, "", 12345],
    )
    def test_bad_commit_is_rejected_with_key_path(self, tmp_dir: Path, commit: object) -> None:
        _write_json(tmp_dir, {"repos": {"worker": {"commit": commit, "archived": False}}})
        with pytest.raises(ValueError, match=r"repos\.worker\.commit"):
            read_baseline(tmp_dir)

    def test_missing_commit_is_rejected_with_key_path(self, tmp_dir: Path) -> None:
        _write_json(tmp_dir, {"repos": {"worker": {"archived": False}}})
        with pytest.raises(ValueError, match=r"repos\.worker\.commit"):
            read_baseline(tmp_dir)

    @pytest.mark.parametrize("archived", ["true", 1, None, "false"])
    def test_archived_must_be_a_real_boolean(self, tmp_dir: Path, archived: object) -> None:
        _write_json(tmp_dir, {"repos": {"worker": {"commit": SHA, "archived": archived}}})
        with pytest.raises(ValueError, match=r"repos\.worker\.archived"):
            read_baseline(tmp_dir)

    def test_bad_list_item_has_index_in_path(self, tmp_dir: Path) -> None:
        _write_json(tmp_dir, {"raw_processed": ["a.md", 3]})
        with pytest.raises(ValueError, match=r"raw_processed\.1"):
            read_baseline(tmp_dir)

    def test_bad_updated_at_type(self, tmp_dir: Path) -> None:
        _write_json(tmp_dir, {"updated_at": 5})
        with pytest.raises(ValueError, match="updated_at"):
            read_baseline(tmp_dir)

    def test_repos_must_be_an_object(self, tmp_dir: Path) -> None:
        _write_json(tmp_dir, {"repos": []})
        with pytest.raises(ValueError, match="repos"):
            read_baseline(tmp_dir)

    def test_all_problems_are_listed(self, tmp_dir: Path) -> None:
        _write_json(
            tmp_dir,
            {"repos": {"a": {"commit": "x", "archived": False}, "b": {"commit": SHA}}},
        )
        with pytest.raises(ValueError) as info:
            read_baseline(tmp_dir)
        message = str(info.value)
        assert "repos.a.commit" in message
        assert "repos.b.archived" in message

    @pytest.mark.parametrize("top", [[], None, "text", 3, True])
    def test_non_object_top_level_uses_root_path(self, tmp_dir: Path, top: object) -> None:
        _write_json(tmp_dir, top)
        with pytest.raises(ValueError) as info:
            read_baseline(tmp_dir)
        message = str(info.value)
        assert message.startswith("baseline.json 형식 오류: (root): ")
        assert ": : " not in message

    def test_broken_json_has_distinct_message(self, tmp_dir: Path) -> None:
        _write_raw(tmp_dir, "{")
        with pytest.raises(ValueError, match=r"baseline\.json이 JSON이 아닙니다") as info:
            read_baseline(tmp_dir)
        assert "형식 오류" not in str(info.value)

    def test_empty_file_is_not_json(self, tmp_dir: Path) -> None:
        _write_raw(tmp_dir, "")
        with pytest.raises(ValueError, match="JSON이 아닙니다"):
            read_baseline(tmp_dir)

    def test_deeply_nested_json_is_reported_not_crashed(self, tmp_dir: Path) -> None:
        _write_raw(tmp_dir, "[" * 100000)
        with pytest.raises(ValueError, match="JSON이 아닙니다"):
            read_baseline(tmp_dir)

    def test_non_utf8_file_is_a_value_error(self, tmp_dir: Path) -> None:
        _write_raw(tmp_dir, b"\xff\xfe{}")
        with pytest.raises(ValueError, match="baseline.json"):
            read_baseline(tmp_dir)


class TestWriteBaseline:
    def test_write_then_read_round_trips(self, tmp_dir: Path) -> None:
        b = Baseline(
            repos={"worker": RepoBaseline(commit=SHA, archived=False)},
            raw_processed=["a.md"],
            updated_at="2026-10-07T09:00:00+09:00",
        )
        write_baseline(tmp_dir, b)
        assert read_baseline(tmp_dir) == b
        assert (tmp_dir / "baseline.json").read_text(encoding="utf-8").endswith("\n")

    def test_indent_two_and_key_order(self, tmp_dir: Path) -> None:
        write_baseline(
            tmp_dir,
            Baseline(
                repos={"w": RepoBaseline(commit=SHA, archived=True)},
                raw_processed=["회의.md"],
                updated_at=None,
            ),
        )
        assert (tmp_dir / "baseline.json").read_text(encoding="utf-8") == (
            "{\n"
            '  "repos": {\n'
            '    "w": {\n'
            f'      "commit": "{SHA}",\n'
            '      "archived": true\n'
            "    }\n"
            "  },\n"
            '  "raw_processed": [\n'
            '    "회의.md"\n'
            "  ],\n"
            '  "updated_at": null\n'
            "}\n"
        )

    def test_creates_directory_and_leaves_no_temp_files(self, tmp_dir: Path) -> None:
        data_dir = tmp_dir / "new" / "data"
        write_baseline(data_dir, empty_baseline())
        assert [p.name for p in data_dir.iterdir()] == ["baseline.json"]
        assert read_baseline(data_dir) == empty_baseline()


class TestWriteBaselineValidates:
    def _good(self) -> Baseline:
        return Baseline(
            repos={"worker": RepoBaseline(commit=SHA, archived=False)},
            raw_processed=["2026-10-07-a.md"],
            updated_at="2026-10-07T09:00:00+09:00",
        )

    def test_valid_data_round_trips(self, tmp_dir: Path) -> None:
        write_baseline(tmp_dir, self._good())
        assert read_baseline(tmp_dir) == self._good()

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda b: b.repos.update({"w": RepoBaseline(commit="abc", archived=False)}),
            lambda b: b.repos.update({"w": RepoBaseline(commit="A" * 40, archived=False)}),
            lambda b: b.repos.update({"w": RepoBaseline(commit=SHA, archived=1)}),  # type: ignore[arg-type]
            lambda b: b.repos.update({7: RepoBaseline(commit=SHA, archived=False)}),
            lambda b: b.raw_processed.append(5),
            lambda b: setattr(b, "updated_at", 123),
        ],
    )
    def test_invalid_data_raises_value_error_and_keeps_the_existing_file(
        self, tmp_dir: Path, mutate: Any
    ) -> None:
        write_baseline(tmp_dir, self._good())
        before = (tmp_dir / "baseline.json").read_bytes()
        bad = self._good()
        mutate(bad)
        with pytest.raises(ValueError, match="baseline.json"):
            write_baseline(tmp_dir, bad)
        assert (tmp_dir / "baseline.json").read_bytes() == before
        assert [p.name for p in tmp_dir.iterdir()] == ["baseline.json"]  # 임시 파일도 없다

    def test_invalid_data_does_not_create_a_file(self, tmp_dir: Path) -> None:
        bad = Baseline(repos={"w": RepoBaseline(commit="nope", archived=False)})
        with pytest.raises(ValueError):
            write_baseline(tmp_dir, bad)
        assert list(tmp_dir.iterdir()) == []
