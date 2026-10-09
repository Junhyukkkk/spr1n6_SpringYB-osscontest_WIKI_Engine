import math
import random
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from team_wiki_engine.wiki.frontmatter import (
    MAX_FRONTMATTER_CHARS,
    ParsedFrontmatter,
    parse_frontmatter,
    serialize_frontmatter,
)


class TestParseFrontmatter:
    def test_reads_key_values_and_keeps_dates_as_strings(self) -> None:
        p = parse_frontmatter(
            "---\nstatus: 확정\nupdated: 2026-10-07\nsources: [a.md]\n---\n\n# 본문\n"
        )
        assert p.error is None
        assert p.data == {"status": "확정", "updated": "2026-10-07", "sources": ["a.md"]}
        assert p.body == "\n# 본문\n"

    def test_handles_crlf_and_bom(self) -> None:
        p = parse_frontmatter("\ufeff---\r\nupdated: 2026-10-07\r\n---\r\n본문")
        assert p.data == {"updated": "2026-10-07"}
        assert p.error is None
        assert p.body == "본문"

    def test_no_frontmatter(self) -> None:
        assert parse_frontmatter("# 본문만") == ParsedFrontmatter(
            data=None, body="# 본문만", error=None
        )

    def test_only_first_closing_fence_ends_frontmatter(self) -> None:
        p = parse_frontmatter("---\na: 1\n---\n앞\n---\n뒤\n---\n")
        assert p.data == {"a": 1}
        assert p.body == "앞\n---\n뒤\n---\n"

    def test_empty_frontmatter_is_empty_mapping(self) -> None:
        assert parse_frontmatter("---\n---\n본문") == ParsedFrontmatter(
            data={}, body="본문", error=None
        )
        assert parse_frontmatter("---\r\n---\r\n본문").data == {}
        assert parse_frontmatter("---\n---").data == {}

    def test_blank_or_comment_only_frontmatter_is_empty_mapping(self) -> None:
        assert parse_frontmatter("---\n\n---\n본문").data == {}
        assert parse_frontmatter("---\n# 주석만\n---\n본문").data == {}

    def test_unclosed_frontmatter_is_plain_body(self) -> None:
        text = "---\nstatus: 확정\n본문 계속"
        assert parse_frontmatter(text) == ParsedFrontmatter(data=None, body=text, error=None)
        assert parse_frontmatter("---").data is None
        assert parse_frontmatter("---\n").error is None

    def test_fence_must_start_the_text(self) -> None:
        text = "\n---\na: 1\n---\n"
        assert parse_frontmatter(text) == ParsedFrontmatter(data=None, body=text, error=None)

    def test_closing_fence_allows_trailing_spaces_only(self) -> None:
        assert parse_frontmatter("---\na: 1\n---  \n본문").body == "본문"
        text = "---\na: 1\n---x\n본문"
        assert parse_frontmatter(text).data is None

    def test_yaml_error_is_reported_on_one_line(self) -> None:
        p = parse_frontmatter("---\nkey: [닫히지 않음\n---\n본문")
        assert p.data is None
        assert p.error is not None
        assert p.error.startswith("frontmatter YAML 오류: ")
        assert "\n" not in p.error
        assert p.body == "본문"

    def test_non_mapping_frontmatter_is_reported(self) -> None:
        for text in ("---\n- a\n- b\n---\n", "---\nhello\n---\n", "---\n~\n---\n", "---\n3\n---\n"):
            p = parse_frontmatter(text)
            assert p.data is None
            assert p.error == "frontmatter가 키-값 형식이 아닙니다"

    def test_deeply_nested_yaml_is_an_error_not_a_crash(self) -> None:
        p = parse_frontmatter("---\nk: " + "[" * 20000 + "\n---\n")
        assert p.data is None
        assert p.error is not None

    def test_dates_and_datetimes_stay_strings(self) -> None:
        p = parse_frontmatter(
            "---\nupdated: 2026-10-07\nt: 2026-10-07T09:00:00+09:00\n"
            "u: 2026-10-07 09:00:00\nl: [2026-10-07]\n---\n"
        )
        assert p.data == {
            "updated": "2026-10-07",
            "t": "2026-10-07T09:00:00+09:00",
            "u": "2026-10-07 09:00:00",
            "l": ["2026-10-07"],
        }

    def test_yaml_1_1_booleans_stay_strings(self) -> None:
        p = parse_frontmatter("---\nstatus: no\nowners: [on]\na: yes\nb: Off\nc: y\nd: N\n---\n")
        assert p.data == {
            "status": "no",
            "owners": ["on"],
            "a": "yes",
            "b": "Off",
            "c": "y",
            "d": "N",
        }

    def test_true_false_variants_are_booleans(self) -> None:
        p = parse_frontmatter(
            "---\na: true\nb: false\nc: True\nd: False\ne: TRUE\nf: FALSE\ng: tRuE\n---\n"
        )
        assert p.data == {
            "a": True,
            "b": False,
            "c": True,
            "d": False,
            "e": True,
            "f": False,
            "g": "tRuE",
        }

    def test_other_scalar_types_still_resolve(self) -> None:
        p = parse_frontmatter("---\nn: 123\nf: 1.5\nz: null\ne:\nt: ~\ns: '123'\n---\n")
        assert p.data == {"n": 123, "f": 1.5, "z": None, "e": None, "t": None, "s": "123"}

    def test_clock_times_are_not_sexagesimal_numbers(self) -> None:
        p = parse_frontmatter("---\nat: 12:30\nratio: 1:30.5\n---\n")
        assert p.data == {"at": "12:30", "ratio": "1:30.5"}

    def test_non_ascii_keys_and_values(self) -> None:
        assert parse_frontmatter("---\n상태: 확정\n---\n").data == {"상태": "확정"}

    def test_stock_pyyaml_is_unchanged(self) -> None:
        assert yaml.safe_load("a: 2026-10-07\nb: no") == {"a": date(2026, 10, 7), "b": False}

    def test_source_has_no_literal_bom(self) -> None:
        source = Path(__file__).parents[2] / "src" / "team_wiki_engine" / "wiki" / "frontmatter.py"
        assert "\ufeff" not in source.read_text(encoding="utf-8")


class TestSerializeFrontmatter:
    def test_round_trips(self) -> None:
        text = serialize_frontmatter({"updated": "2026-10-07", "owners": ["member-a"]}, "# 본문\n")
        assert parse_frontmatter(text).data == {"updated": "2026-10-07", "owners": ["member-a"]}
        assert text.endswith("# 본문\n")

    def test_exact_layout(self) -> None:
        assert serialize_frontmatter({"a": "b"}, "x") == "---\na: b\n---\n\nx"
        assert serialize_frontmatter({"a": "b"}, "\nx") == "---\na: b\n---\n\nx"

    def test_date_like_strings_are_not_quoted(self) -> None:
        data = {"updated": "2026-10-07", "t": "2026-10-07T09:00:00+09:00"}
        text = serialize_frontmatter(data, "")
        assert "updated: 2026-10-07\n" in text
        assert "t: 2026-10-07T09:00:00+09:00\n" in text

    def test_ambiguous_strings_are_quoted_and_round_trip(self) -> None:
        data: dict[str, Any] = {
            "a": "true",
            "b": "123",
            "c": "2026-10-07",
            "d": "1.5",
            "e": "null",
            "f": "False",
            "g": "~",
            "h": "",
            "i": "0x1f",
            "j": "1_000",
            "k": "12:30",
        }
        text = serialize_frontmatter(data, "")
        assert "a: 'true'" in text
        assert "b: '123'" in text
        assert "c: 2026-10-07" in text
        assert parse_frontmatter(text).data == data

    def test_awkward_strings_round_trip(self) -> None:
        data: dict[str, Any] = {
            "colon": "a: b",
            "hash": "# not a comment",
            "dash": "- item",
            "star": "*alias",
            "amp": "&anchor",
            "at": "@user",
            "bang": "!tag",
            "quote": 'it\'s "quoted"',
            "multi": "line1\nline2\n",
            "space": "  padded  ",
            "korean": "한글: 값",
            "long": "긴 문장 " * 60,
        }
        assert parse_frontmatter(serialize_frontmatter(data, "")).data == data

    def test_nested_values_and_key_order_round_trip(self) -> None:
        data: dict[str, Any] = {
            "z": 1,
            "a": ["x", {"k": "v"}],
            "m": {"n": None, "o": True, "p": 1.5},
            "owners": [],
        }
        parsed = parse_frontmatter(serialize_frontmatter(data, "본문\n")).data
        assert parsed == data
        assert parsed is not None
        assert list(parsed) == ["z", "a", "m", "owners"]

    def test_empty_mapping_round_trips(self) -> None:
        assert parse_frontmatter(serialize_frontmatter({}, "본문\n")).data == {}

    def test_unicode_is_not_escaped(self) -> None:
        assert "상태: 확정" in serialize_frontmatter({"상태": "확정"}, "")

    def test_accepts_any_mapping(self) -> None:
        from types import MappingProxyType

        text = serialize_frontmatter(MappingProxyType({"a": "1"}), "")
        assert parse_frontmatter(text).data == {"a": "1"}

    def test_date_objects_serialize_as_iso_text(self) -> None:
        text = serialize_frontmatter({"updated": date(2026, 10, 7)}, "")
        assert parse_frontmatter(text).data == {"updated": "2026-10-07"}

    @pytest.mark.parametrize("body", ["", "x", "\n# 제목\n", "---\n끝\n"])
    def test_body_survives_round_trip(self, body: str) -> None:
        parsed = parse_frontmatter(serialize_frontmatter({"a": "b"}, body))
        expected = body if body.startswith("\n") else "\n" + body
        assert parsed.body == expected


class TestEmptyFrontmatterBeforeBodyFence:
    @pytest.mark.parametrize(
        ("text", "body"),
        [
            ("---\n---\n본문\n---\n뒤\n", "본문\n---\n뒤\n"),
            ("---\n---\n---\nx", "---\nx"),
            ("---\n---\n# 제목\n\n---\n\n표\n", "# 제목\n\n---\n\n표\n"),
            ("---\r\n---\r\n본문\r\n---\r\n뒤", "본문\r\n---\r\n뒤"),
            ("---\n\n---\n본문\n---\n뒤", "본문\n---\n뒤"),
        ],
    )
    def test_empty_block_then_body_fence(self, text: str, body: str) -> None:
        assert parse_frontmatter(text) == ParsedFrontmatter(data={}, body=body, error=None)

    def test_non_empty_block_still_ends_at_first_fence(self) -> None:
        p = parse_frontmatter("---\na: 1\n---\n---\n뒤\n")
        assert p.data == {"a": 1}
        assert p.body == "---\n뒤\n"

    def test_regex_stays_linear_on_adversarial_input(self) -> None:
        for text in ("---\n" + "a: b\n" * 4_000_000, "---\n" + "---x\n" * 2_000_000):
            started = time.perf_counter()
            p = parse_frontmatter(text)
            assert time.perf_counter() - started < 1.0
            assert p.data is None
            assert p.error is None


class TestAliasesAreRejected:
    def test_alias_bomb_is_rejected_quickly(self) -> None:
        lines = ["l0: &l0 [" + ",".join(["x"] * 9) + "]"]
        for level in range(1, 9):
            refs = ",".join([f"*l{level - 1}"] * 9)
            lines.append(f"l{level}: &l{level} [{refs}]")
        started = time.perf_counter()
        p = parse_frontmatter("---\n" + "\n".join(lines) + "\n---\n")
        assert time.perf_counter() - started < 0.2
        assert p.data is None
        assert p.error == "frontmatter YAML 오류: 앵커·별칭은 쓸 수 없습니다"

    def test_simple_anchor_and_alias(self) -> None:
        p = parse_frontmatter("---\na: &x 1\nb: *x\n---\n")
        assert p.data is None
        assert p.error == "frontmatter YAML 오류: 앵커·별칭은 쓸 수 없습니다"

    def test_anchor_without_alias(self) -> None:
        p = parse_frontmatter("---\na: &x 1\n---\n")
        assert p.data is None
        assert p.error == "frontmatter YAML 오류: 앵커·별칭은 쓸 수 없습니다"

    def test_merge_key_with_alias(self) -> None:
        p = parse_frontmatter("---\nbase: &base {a: 1}\nd:\n  <<: *base\n---\n")
        assert p.data is None
        assert p.error is not None
        assert p.error.startswith("frontmatter YAML 오류: ")

    def test_inline_merge_key_without_alias(self) -> None:
        p = parse_frontmatter("---\nd:\n  <<: {a: 1}\n---\n")
        assert p.data is None
        assert p.error == "frontmatter YAML 오류: 병합 키(<<)는 쓸 수 없습니다"

    def test_normal_frontmatter_still_parses(self) -> None:
        p = parse_frontmatter("---\nstatus: 확정\nrefs: [a.md, b.md]\nnested: {k: v}\n---\n")
        assert p.error is None
        assert p.data == {"status": "확정", "refs": ["a.md", "b.md"], "nested": {"k": "v"}}

    def test_ampersand_and_star_inside_strings_are_fine(self) -> None:
        p = parse_frontmatter('---\na: "&x *y"\nb: x&y\n---\n')
        assert p.data == {"a": "&x *y", "b": "x&y"}


class TestSizeLimit:
    def _block(self, size: int) -> str:
        # 정확히 size글자의 YAML 소스: "k: " + 값 (줄바꿈은 포함하지 않는다)
        return "k: " + "x" * (size - 3)

    def test_limit_constant(self) -> None:
        assert MAX_FRONTMATTER_CHARS == 65_536

    def test_exactly_at_limit_parses(self) -> None:
        source = self._block(MAX_FRONTMATTER_CHARS)
        p = parse_frontmatter(f"---\n{source}\n---\n본문")
        assert p.error is None
        assert p.data == {"k": "x" * (MAX_FRONTMATTER_CHARS - 3)}

    def test_one_over_limit_is_an_error(self) -> None:
        source = self._block(MAX_FRONTMATTER_CHARS + 1)
        started = time.perf_counter()
        p = parse_frontmatter(f"---\n{source}\n---\n본문")
        assert time.perf_counter() - started < 0.5
        assert p == ParsedFrontmatter(
            data=None, body="본문", error="frontmatter가 너무 큽니다 (최대 64KB)"
        )

    def test_two_megabyte_block_returns_quickly(self) -> None:
        text = "---\n" + "a: b\n" * 400_000 + "---\n본문"
        started = time.perf_counter()
        p = parse_frontmatter(text)
        assert time.perf_counter() - started < 0.5
        assert p.data is None
        assert p.error == "frontmatter가 너무 큽니다 (최대 64KB)"
        assert p.body == "본문"


class TestOnlyJsonLikeData:
    @pytest.mark.parametrize(
        "block",
        [
            "1: x",
            "true: x",
            "null: x",
            "~: x",
            "1.5: x",
            "a:\n  1: x",
            "l:\n  - {2: x}",
        ],
    )
    def test_non_string_keys_are_rejected(self, block: str) -> None:
        p = parse_frontmatter(f"---\n{block}\n---\n")
        assert p.data is None
        assert p.error is not None
        assert p.error == "frontmatter의 키는 문자열이어야 합니다"

    def test_unhashable_key_is_a_yaml_error(self) -> None:
        p = parse_frontmatter("---\nk: [{[]: 1}]\n---\n")
        assert p.data is None
        assert p.error is not None
        assert p.error.startswith("frontmatter YAML 오류: ")

    def test_non_string_key_message(self) -> None:
        assert (
            parse_frontmatter("---\na:\n  - {2: x}\n---\n").error
            == "frontmatter의 키는 문자열이어야 합니다"
        )
        assert (
            parse_frontmatter("---\n1: x\n---\n").error == "frontmatter의 키는 문자열이어야 합니다"
        )

    @pytest.mark.parametrize(
        "block",
        [
            "a: !!timestamp 2026-10-07",
            "a: !!binary aGk=",
            "a: !!set {x, y}",
            "a: !!omap [x: 1]",
            "a: !!pairs [x: 1]",
            "a: !!python/object/apply:os.getcwd []",
        ],
    )
    def test_explicit_non_json_tags_are_rejected(self, block: str) -> None:
        p = parse_frontmatter(f"---\n{block}\n---\n")
        assert p.data is None
        assert p.error is not None
        assert p.error.startswith("frontmatter YAML 오류: ")

    def test_nested_values_of_allowed_types_parse(self) -> None:
        p = parse_frontmatter(
            "---\na: [1, 2.5, true, null, x, {k: [v]}]\nb:\n  c: {d: false}\n---\n"
        )
        assert p.data == {"a": [1, 2.5, True, None, "x", {"k": ["v"]}], "b": {"c": {"d": False}}}

    def test_nan_and_infinity_floats_are_kept(self) -> None:
        # JSON으로 쓸 때 NaN은 비표준이고 NaN != NaN이므로 `==` 비교를 기대하면 안 된다.
        p = parse_frontmatter("---\na: .nan\nb: .inf\n---\n")
        assert p.data is not None
        assert math.isnan(p.data["a"])
        assert p.data["b"] == math.inf

    def test_five_thousand_deep_flow_mapping_is_an_error_not_a_crash(self) -> None:
        block = "{a: " * 5000 + "1" + "}" * 5000
        p = parse_frontmatter(f"---\nk: {block}\n---\n")
        assert p.data is None
        assert p.error is not None

    def test_moderately_deep_valid_nesting_still_works(self) -> None:
        block = "[" * 50 + "1" + "]" * 50
        p = parse_frontmatter(f"---\nk: {block}\n---\n")
        assert p.error is None

    def test_non_string_key_at_depth_fifty(self) -> None:
        block = "{a: " * 50 + "{1: x}" + "}" * 50
        p = parse_frontmatter(f"---\nk: {block}\n---\n")
        assert p.data is None
        assert p.error == "frontmatter의 키는 문자열이어야 합니다"

    def test_explicit_tag_at_depth_fifty(self) -> None:
        block = "[" * 50 + "!!set {a}" + "]" * 50
        p = parse_frontmatter(f"---\nk: {block}\n---\n")
        assert p.data is None
        assert p.error is not None
        assert p.error.startswith("frontmatter YAML 오류: ")


class TestNeverRaises:
    MAX_ERROR_CHARS = 260

    @pytest.mark.parametrize(
        "block",
        [
            "a: " + "1" * 5000,
            "a: -" + "1" * 5000,
            "a: !!int " + "1" * 5000,
            "a: !!int abc",
            "a: !<tag:yaml.org,2002:int> zz",
            "a: !!float abc",
            "a: !!bool maybe",
            "a: !!bool ''",
            "a: !!int ''",
            "a: !!float ''",
            "a: !!int 0x",
            "a: !!int 0b",
        ],
    )
    def test_malformed_scalars_are_errors_not_exceptions(self, block: str) -> None:
        started = time.perf_counter()
        p = parse_frontmatter(f"---\n{block}\n---\n본문")
        assert time.perf_counter() - started < 1.0
        assert p.data is None
        assert p.body == "본문"
        assert p.error is not None
        assert p.error.startswith("frontmatter YAML 오류: ")
        assert len(p.error) <= self.MAX_ERROR_CHARS

    def test_non_yaml_exceptions_show_class_name_only(self) -> None:
        p = parse_frontmatter("---\na: !!int " + "1" * 5000 + "\n---\n")
        assert p.error is not None
        assert "ValueError" in p.error
        assert "1111" not in p.error

    def test_huge_tag_text_is_truncated_in_the_error(self) -> None:
        p = parse_frontmatter("---\na: !" + "t" * 60_000 + " x\n---\n")
        assert p.data is None
        assert p.error is not None
        assert len(p.error) <= self.MAX_ERROR_CHARS

    def test_keyboard_interrupt_still_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import team_wiki_engine.wiki.frontmatter as module

        def interrupt(source: str) -> object:
            raise KeyboardInterrupt

        monkeypatch.setattr(module, "_load_mapping_source", interrupt)
        with pytest.raises(KeyboardInterrupt):
            parse_frontmatter("---\na: 1\n---\n")

    def test_fuzz_never_raises(self) -> None:
        atoms = [
            "!!int", "!!bool", "!!float", "!!str", "!!set", "!!binary", "!", "!<", ">", "1", "0",
            "123456789", "9" * 300, "-", "+", "0x", "0b", "1_", ".", "e", ".nan", "&", "*", "&a",
            "*a", "<<", "?", ":", ": ", "- ", "'", '"', "\\", "#", "[", "]", "{", "}", ",", "|",
            ">-", "\t", "\x00", "\ud800", "\ufeff", "\n", "\r\n", " ", "  ", "a", "key", "true",
            "null", "~", "yes", "---", "...", "%", "@", "`", "\u0085", "\u2028", "한글",
        ]  # fmt: skip
        rng = random.Random(20261007)
        started = time.perf_counter()
        for _ in range(3000):
            source = "".join(rng.choice(atoms) for _ in range(rng.randint(1, 25)))
            for text in (f"---\n{source}\n---\n본문", f"---\na: {source}\n---\n본문"):
                result = parse_frontmatter(text)
                assert isinstance(result, ParsedFrontmatter)
                assert result.data is None or result.error is None
        assert time.perf_counter() - started < 20
