import unicodedata
from pathlib import Path

import pytest

from team_wiki_engine.claude.types import ToolCallTrace
from team_wiki_engine.metrics.answer_source import answer_source, code_paths, code_reads


def _call(path: Path | None, *, name: str = "Read", allowed: bool = True) -> ToolCallTrace:
    return ToolCallTrace(name=name, path=path, allowed=allowed)


@pytest.fixture
def repos(tmp_dir: Path) -> Path:
    root = tmp_dir / "repos"
    (root / "app" / "src").mkdir(parents=True)
    (root / "app" / "src" / "Main.kt").write_text("x", encoding="utf-8")
    return root


class TestAnswerSource:
    def test_no_calls_is_wiki(self, repos: Path) -> None:
        assert answer_source([], repos) == "wiki"
        assert code_paths([], repos) == []
        assert code_reads([], repos) == []

    @pytest.mark.parametrize("name", ["Read", "Grep", "Glob"])
    def test_read_tools_inside_repos_are_code(self, repos: Path, name: str) -> None:
        calls = [_call(repos / "app" / "src" / "Main.kt", name=name)]
        assert answer_source(calls, repos) == "code"
        assert code_paths(calls, repos) == ["app/src/Main.kt"]

    def test_repos_root_itself_is_a_code_read_without_a_path(self, repos: Path) -> None:
        calls = [_call(repos, name="Grep")]
        assert answer_source(calls, repos) == "code"
        assert code_paths(calls, repos) == []

    def test_path_outside_repos_is_wiki(self, tmp_dir: Path, repos: Path) -> None:
        (tmp_dir / "wiki").mkdir()
        calls = [_call(tmp_dir / "wiki" / "a.md")]
        assert answer_source(calls, repos) == "wiki"

    def test_sibling_prefix_directory_is_wiki(self, tmp_dir: Path, repos: Path) -> None:
        evil = tmp_dir / "repos-evil"
        evil.mkdir()
        (evil / "x.kt").write_text("x", encoding="utf-8")
        calls = [_call(evil / "x.kt")]
        assert answer_source(calls, repos) == "wiki"
        assert code_paths(calls, repos) == []

    def test_sibling_prefix_that_does_not_exist_is_wiki(self, tmp_dir: Path, repos: Path) -> None:
        assert answer_source([_call(tmp_dir / "repos-evil" / "x.kt")], repos) == "wiki"

    def test_ignored_calls(self, repos: Path) -> None:
        inside = repos / "app" / "src" / "Main.kt"
        calls = [
            _call(inside, allowed=False),
            _call(inside, name="Write"),
            _call(inside, name="Edit"),
            _call(None),
            _call(None, name="Glob"),
        ]
        assert answer_source(calls, repos) == "wiki"
        assert code_paths(calls, repos) == []

    def test_relative_trace_path_is_not_a_code_read(
        self, repos: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(repos)  # 상대 경로가 cwd 기준으로는 코드 사본 안을 가리키게 한다
        assert (repos / "app" / "src" / "Main.kt").exists()
        calls = [_call(Path("app/src/Main.kt"))]
        assert answer_source(calls, repos) == "wiki"
        assert code_paths(calls, repos) == []

    def test_alias_repos_dir_and_real_path_in_record(self, tmp_dir: Path) -> None:
        real = tmp_dir / "real" / "repos"
        real.mkdir(parents=True)
        (real / "x.kt").write_text("x", encoding="utf-8")
        alias = tmp_dir / "alias"
        alias.symlink_to(real, target_is_directory=True)
        calls = [_call(real / "x.kt")]
        assert answer_source(calls, alias) == "code"
        assert code_paths(calls, alias) == ["x.kt"]

    def test_real_repos_dir_and_alias_path_in_record(self, tmp_dir: Path) -> None:
        real = tmp_dir / "real" / "repos"
        real.mkdir(parents=True)
        (real / "x.kt").write_text("x", encoding="utf-8")
        alias = tmp_dir / "alias"
        alias.symlink_to(real, target_is_directory=True)
        calls = [_call(alias / "x.kt")]
        assert answer_source(calls, real) == "code"
        assert code_paths(calls, real) == ["x.kt"]

    def test_symlink_inside_root_pointing_outside_is_wiki(self, tmp_dir: Path, repos: Path) -> None:
        outside = tmp_dir / "outside"
        outside.mkdir()
        (outside / "secret.kt").write_text("x", encoding="utf-8")
        (repos / "link").symlink_to(outside, target_is_directory=True)
        (repos / "flink.kt").symlink_to(outside / "secret.kt")
        calls = [_call(repos / "link" / "secret.kt"), _call(repos / "flink.kt")]
        assert answer_source(calls, repos) == "wiki"
        assert code_paths(calls, repos) == []

    def test_symlink_inside_root_pointing_inside_is_code_with_real_relative_path(
        self, repos: Path
    ) -> None:
        (repos / "short").symlink_to(repos / "app" / "src", target_is_directory=True)
        calls = [_call(repos / "short" / "Main.kt")]
        assert answer_source(calls, repos) == "code"
        assert code_paths(calls, repos) == ["app/src/Main.kt"]

    def test_symlink_cycle_is_wiki_without_exception(self, tmp_dir: Path, repos: Path) -> None:
        (repos / "a").symlink_to(repos / "b")
        (repos / "b").symlink_to(repos / "a")
        calls = [_call(repos / "a" / "x.kt"), _call(repos / "a")]
        assert answer_source(calls, repos) == "wiki"
        assert code_paths(calls, repos) == []
        assert code_reads(calls, repos) == []

    def test_cycle_call_does_not_hide_other_code_reads(self, repos: Path) -> None:
        (repos / "a").symlink_to(repos / "b")
        (repos / "b").symlink_to(repos / "a")
        calls = [_call(repos / "a" / "x.kt"), _call(repos / "app" / "src" / "Main.kt")]
        assert answer_source(calls, repos) == "code"
        assert code_paths(calls, repos) == ["app/src/Main.kt"]

    def test_repos_dir_that_is_a_cycle_is_wiki(self, tmp_dir: Path) -> None:
        (tmp_dir / "a").symlink_to(tmp_dir / "b")
        (tmp_dir / "b").symlink_to(tmp_dir / "a")
        calls = [_call(tmp_dir / "a" / "x.kt")]
        assert answer_source(calls, tmp_dir / "a") == "wiki"
        assert code_paths(calls, tmp_dir / "a") == []

    def test_nul_in_path_does_not_raise(self, repos: Path) -> None:
        calls = [_call(Path(str(repos / "app") + "\x00/x.kt"))]
        assert answer_source(calls, repos) == "wiki"
        assert code_paths(calls, repos) == []


class TestCodePaths:
    def test_dedup_order_and_slash_separated(self, repos: Path) -> None:
        (repos / "b").mkdir()
        calls = [
            _call(repos / "b" / "z.kt"),
            _call(repos / "app" / "src" / "Main.kt"),
            _call(repos / "b" / "z.kt", name="Grep"),
            _call(repos / "app" / "src" / "Main.kt", name="Read"),
        ]
        assert code_paths(calls, repos) == ["b/z.kt", "app/src/Main.kt"]

    def test_nfc_normalized(self, repos: Path) -> None:
        nfc = unicodedata.normalize("NFC", "한글/파일.kt")
        nfd = unicodedata.normalize("NFD", nfc)
        # 존재하지 않는 경로라 파일시스템의 정규화 규칙에 기대지 않고 문자열만 확인한다.
        calls = [_call(repos / nfd), _call(repos / nfc)]
        assert code_paths(calls, repos) == [nfc]

    def test_nonexistent_file_inside_repos_counts(self, repos: Path) -> None:
        calls = [_call(repos / "deleted" / "Old.kt")]
        assert answer_source(calls, repos) == "code"
        assert code_paths(calls, repos) == ["deleted/Old.kt"]

    def test_code_reads_returns_only_code_read_paths(self, repos: Path, tmp_dir: Path) -> None:
        inside = repos / "app" / "src" / "Main.kt"
        calls = [_call(tmp_dir / "wiki" / "a.md"), _call(inside), _call(inside, allowed=False)]
        assert code_reads(calls, repos) == [inside]
