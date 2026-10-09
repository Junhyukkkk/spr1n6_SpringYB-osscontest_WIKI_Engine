import dataclasses
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from team_wiki_engine.claude.profiles import DISALLOWED_TOOLS, PROFILES, ClaudeProfile
from team_wiki_engine.claude.prompt import (
    build_system_append,
    clear_prompt_cache,
    strip_frontmatter,
)
from team_wiki_engine.claude.types import ClaudeProfileName
from tests.helpers import make_tmp_dir, write_tree

WIKI_SCHEMA = "---\nname: wiki-schema\ndescription: d\n---\n\n스키마 본문\n"
ASK_CRLF = "---\r\nname: ask\r\ndescription: d\r\n---\r\n\r\n질문 본문\r\n"
SYNC_CODE = "---\nname: sync-code\ndescription: d\n---\n코드 본문"
INGEST = "---\nname: ingest\ndescription: d\n---\n문서 본문"


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clear_prompt_cache()
    yield
    clear_prompt_cache()


@pytest.fixture
def skills(tmp_dir: Path) -> Path:
    # CRLF가 그대로 디스크에 닿도록 바이트로 쓴다(write_tree는 텍스트 모드).
    write_tree(
        tmp_dir,
        {
            "wiki-schema/SKILL.md": WIKI_SCHEMA,
            "sync-code/SKILL.md": SYNC_CODE,
            "ingest/SKILL.md": INGEST,
        },
    )
    (tmp_dir / "ask").mkdir()
    (tmp_dir / "ask" / "SKILL.md").write_bytes(ASK_CRLF.encode("utf-8"))
    return tmp_dir


class TestProfiles:
    def test_bot_uses_read_only_tools(self) -> None:
        bot = PROFILES["bot"]
        assert bot.name == "bot"
        assert bot.tools == ("Read", "Grep", "Glob")
        assert bot.skills == ("wiki-schema", "ask")
        assert bot.effort_key == "bot"
        assert bot.max_turns == 30

    @pytest.mark.parametrize("name", ["sync-code", "ingest"])
    def test_sync_profiles_edit_and_write_with_sync_effort(self, name: ClaudeProfileName) -> None:
        profile = PROFILES[name]
        assert profile.name == name
        assert profile.tools == ("Read", "Grep", "Glob", "Edit", "Write")
        assert profile.skills == ("wiki-schema", name)
        assert profile.effort_key == "sync"
        assert profile.max_turns == 80

    def test_disallowed_tools_block_exec_web_subagent_skill_and_env_rules(self) -> None:
        assert DISALLOWED_TOOLS == (
            "Bash",
            "NotebookEdit",
            "WebFetch",
            "WebSearch",
            "Agent",
            "Task",
            "Skill",
            "Read(**/.env)",
            "Grep(**/.env)",
        )

    def test_profiles_mapping_is_immutable(self) -> None:
        mutable = cast(Any, PROFILES)
        with pytest.raises(TypeError):
            mutable["bot"] = PROFILES["ingest"]
        with pytest.raises(TypeError):
            del mutable["bot"]
        with pytest.raises(TypeError):
            mutable["new"] = PROFILES["bot"]
        assert PROFILES["bot"].name == "bot"

    def test_profile_fields_are_immutable(self) -> None:
        bot = PROFILES["bot"]
        with pytest.raises(dataclasses.FrozenInstanceError):
            cast(Any, bot).max_turns = 999
        with pytest.raises(AttributeError):
            cast(Any, bot.tools).append("Bash")
        assert bot.tools == ("Read", "Grep", "Glob")
        assert isinstance(bot, ClaudeProfile)

    def test_disallowed_tools_is_immutable(self) -> None:
        assert isinstance(DISALLOWED_TOOLS, tuple)
        with pytest.raises(TypeError):
            cast(Any, DISALLOWED_TOOLS)[0] = "Read"
        with pytest.raises(AttributeError):
            cast(Any, DISALLOWED_TOOLS).append("Read")
        assert DISALLOWED_TOOLS[0] == "Bash"


class TestBuildSystemAppend:
    def test_puts_wiki_schema_first_and_strips_frontmatter(self, skills: Path) -> None:
        assert (
            build_system_append("bot", skills)
            == "# wiki-schema\n\n스키마 본문\n\n# ask\n\n질문 본문"
        )
        assert (
            build_system_append("sync-code", skills)
            == "# wiki-schema\n\n스키마 본문\n\n# sync-code\n\n코드 본문"
        )
        assert (
            build_system_append("ingest", skills)
            == "# wiki-schema\n\n스키마 본문\n\n# ingest\n\n문서 본문"
        )

    def test_missing_skill_file_error_names_the_path(self, skills: Path) -> None:
        (skills / "ask" / "SKILL.md").unlink()
        with pytest.raises(FileNotFoundError) as excinfo:
            build_system_append("bot", skills)
        assert str(skills / "ask" / "SKILL.md") in str(excinfo.value)

    def test_missing_skill_directory_error_names_the_path(self, skills: Path) -> None:
        (skills / "ask" / "SKILL.md").unlink()
        (skills / "ask").rmdir()
        with pytest.raises(FileNotFoundError, match=r"ask[\\/]SKILL\.md"):
            build_system_append("bot", skills)

    def test_empty_body_fails_with_the_path(self, skills: Path) -> None:
        target = skills / "ingest" / "SKILL.md"
        target.write_text("---\nname: ingest\ndescription: d\n---\n\n   \n", encoding="utf-8")
        with pytest.raises(ValueError, match="비어 있습니다") as excinfo:
            build_system_append("ingest", skills)
        assert str(target) in str(excinfo.value)

    def test_reads_once_per_process_until_cache_is_cleared(self, skills: Path) -> None:
        first = build_system_append("bot", skills)
        (skills / "ask" / "SKILL.md").write_text(
            "---\nname: ask\ndescription: d\n---\n바뀜", encoding="utf-8"
        )
        assert build_system_append("bot", skills) == first
        clear_prompt_cache()
        assert "바뀜" in build_system_append("bot", skills)

    def test_errors_are_not_cached(self, skills: Path) -> None:
        target = skills / "ingest" / "SKILL.md"
        target.write_text("---\nname: ingest\n---\n", encoding="utf-8")
        with pytest.raises(ValueError, match="비어 있습니다"):
            build_system_append("ingest", skills)
        target.write_text("---\nname: ingest\n---\n고쳐진 본문", encoding="utf-8")
        assert build_system_append("ingest", skills).endswith("# ingest\n\n고쳐진 본문")

        target.unlink()
        clear_prompt_cache()
        with pytest.raises(FileNotFoundError):
            build_system_append("ingest", skills)
        target.write_text(INGEST, encoding="utf-8")
        assert build_system_append("ingest", skills).endswith("# ingest\n\n문서 본문")

    def test_same_profile_with_different_skills_dir_is_cached_separately(
        self, skills: Path
    ) -> None:
        other = make_tmp_dir()
        try:
            write_tree(
                other,
                {
                    "wiki-schema/SKILL.md": "다른 스키마",
                    "ask/SKILL.md": "다른 질문",
                },
            )
            first = build_system_append("bot", skills)
            second = build_system_append("bot", other)
            assert first == "# wiki-schema\n\n스키마 본문\n\n# ask\n\n질문 본문"
            assert second == "# wiki-schema\n\n다른 스키마\n\n# ask\n\n다른 질문"
            assert build_system_append("bot", skills) == first
            assert build_system_append("bot", other) == second
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_bom_and_crlf_file(self, skills: Path) -> None:
        raw = "\ufeff---\r\nname: ask\r\ndescription: d\r\n---\r\n\r\n첫 줄\r\n둘째 줄\r\n"
        (skills / "ask" / "SKILL.md").write_bytes(raw.encode("utf-8"))
        assert build_system_append("bot", skills) == (
            "# wiki-schema\n\n스키마 본문\n\n# ask\n\n첫 줄\n둘째 줄"
        )

    def test_bom_without_frontmatter_leaves_no_bom_in_output(self, skills: Path) -> None:
        (skills / "ask" / "SKILL.md").write_bytes("\ufeff그냥 본문\r\n".encode())
        result = build_system_append("bot", skills)
        assert "\ufeff" not in result
        assert result.endswith("# ask\n\n그냥 본문")

    def test_body_dashes_line_is_preserved(self, skills: Path) -> None:
        body = "앞 문단\n\n---\n\n뒤 문단\n---\n끝"
        (skills / "ask" / "SKILL.md").write_text(
            f"---\nname: ask\ndescription: d\n---\n{body}\n", encoding="utf-8"
        )
        assert build_system_append("bot", skills).endswith(f"# ask\n\n{body}")

    def test_empty_frontmatter_is_stripped(self, skills: Path) -> None:
        (skills / "ask" / "SKILL.md").write_text("---\n---\n본문\n", encoding="utf-8")
        assert build_system_append("bot", skills).endswith("# ask\n\n본문")

    def test_undecodable_file_error_names_the_path(self, skills: Path) -> None:
        target = skills / "ask" / "SKILL.md"
        target.write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(ValueError) as excinfo:
            build_system_append("bot", skills)
        assert str(target) in str(excinfo.value)


class TestStripFrontmatter:
    def test_without_frontmatter_returns_text_unchanged(self) -> None:
        assert strip_frontmatter("본문만") == "본문만"

    def test_strips_standard_frontmatter(self) -> None:
        assert strip_frontmatter("---\nname: x\n---\n본문") == "본문"

    def test_strips_empty_frontmatter(self) -> None:
        assert strip_frontmatter("---\n---\nbody") == "body"
        assert strip_frontmatter("---\r\n---\r\nbody") == "body"
        assert strip_frontmatter("---\n---") == ""

    def test_handles_bom_and_crlf(self) -> None:
        assert strip_frontmatter("\ufeff---\r\nname: x\r\n---\r\n본문\r\n") == "본문\r\n"

    def test_keeps_body_dashes_lines(self) -> None:
        text = "---\nname: x\n---\n앞\n---\n뒤\n---\n"
        assert strip_frontmatter(text) == "앞\n---\n뒤\n---\n"

    def test_no_closing_fence_returns_text_unchanged(self) -> None:
        text = "---\nname: x\n본문 계속"
        assert strip_frontmatter(text) == text
        assert strip_frontmatter("---\n") == "---\n"
        assert strip_frontmatter("---") == "---"

    def test_no_opening_fence_returns_text_unchanged(self) -> None:
        text = "name: x\n---\n본문"
        assert strip_frontmatter(text) == text
        assert strip_frontmatter("앞줄\n---\nname: x\n---\n본문") == "앞줄\n---\nname: x\n---\n본문"

    def test_closing_fence_must_be_a_whole_line(self) -> None:
        text = "---\nname: x\n----\n본문"
        assert strip_frontmatter(text) == text
        assert strip_frontmatter("---\nname: x\n---x\n본문") == "---\nname: x\n---x\n본문"

    def test_allows_trailing_spaces_after_closing_fence(self) -> None:
        assert strip_frontmatter("---\nname: x\n---  \n본문") == "본문"

    def test_regex_source_has_no_literal_bom(self) -> None:
        source = Path(__file__).parents[2] / "src" / "team_wiki_engine" / "claude" / "prompt.py"
        assert "\ufeff" not in source.read_text(encoding="utf-8")
