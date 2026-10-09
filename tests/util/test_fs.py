import os
import time
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest

from team_wiki_engine.util.fs import (
    DirCache,
    find_symlinks,
    list_markdown_files,
    read_text_if_exists,
    resolve_normalized,
    write_file_atomic,
)
from tests.helpers import write_tree
from tests.helpers.hang_guard import hang_guard, open_fd_count

NFC_NAME = unicodedata.normalize("NFC", "2026-10-07-회의록.md")
NFD_NAME = unicodedata.normalize("NFD", NFC_NAME)
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _fake_listing(monkeypatch: pytest.MonkeyPatch, directory: Path, names: list[str]) -> None:
    """`directory`의 `os.listdir` 결과만 `names`로 바꾼다."""
    real_listdir = os.listdir

    def fake(path: str | os.PathLike[str]) -> list[str]:
        if os.fspath(path) == str(directory):
            return list(names)
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", fake)


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


class TestResolveNormalized:
    def test_nfd_stored_file_found_by_nfc_name(self, tmp_dir: Path) -> None:
        (tmp_dir / NFD_NAME).write_text("x", encoding="utf-8")
        found = resolve_normalized(tmp_dir / NFC_NAME)
        assert found is not None
        assert found.read_text(encoding="utf-8") == "x"

    @pytest.mark.parametrize("stored_nfd", [True, False])
    @pytest.mark.parametrize("lookup_nfd", [True, False])
    def test_any_stored_and_lookup_form_combination(
        self, tmp_dir: Path, stored_nfd: bool, lookup_nfd: bool
    ) -> None:
        (tmp_dir / (NFD_NAME if stored_nfd else NFC_NAME)).write_text("x", encoding="utf-8")
        found = resolve_normalized(tmp_dir / (NFD_NAME if lookup_nfd else NFC_NAME))
        assert found is not None
        assert _nfc(found.name) == NFC_NAME
        assert found.read_text(encoding="utf-8") == "x"

    def test_nfd_subdirectory_component(self, tmp_dir: Path) -> None:
        folder_nfc = unicodedata.normalize("NFC", "회의록")
        folder_nfd = unicodedata.normalize("NFD", folder_nfc)
        (tmp_dir / folder_nfd).mkdir()
        (tmp_dir / folder_nfd / "a.md").write_text("y", encoding="utf-8")
        found = resolve_normalized(tmp_dir / folder_nfc / "a.md")
        assert found is not None
        assert found.read_text(encoding="utf-8") == "y"
        assert _nfc(found.parent.name) == folder_nfc

    def test_missing_file_is_none(self, tmp_dir: Path) -> None:
        assert resolve_normalized(tmp_dir / "none.md") is None

    def test_missing_intermediate_directory_is_none(self, tmp_dir: Path) -> None:
        assert resolve_normalized(tmp_dir / "no" / "such" / "a.md") is None

    def test_root_resolves_to_itself(self) -> None:
        assert resolve_normalized(Path("/")) == Path("/")

    def test_relative_input_becomes_absolute(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_tree(tmp_dir, {"sub/f.md": "z"})
        monkeypatch.chdir(tmp_dir)
        assert resolve_normalized(Path("sub/f.md")) == tmp_dir / "sub" / "f.md"

    def test_returns_given_prefix_without_realpath(self, raw_tmp_dir: Path) -> None:
        write_tree(raw_tmp_dir, {"x.md": ""})
        assert resolve_normalized(raw_tmp_dir / "x.md") == raw_tmp_dir / "x.md"

    def test_exact_name_wins_when_both_forms_exist(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # APFS는 NFC/NFD를 접기 때문에 실제 파일 대신 목록을 흉내 내어 두 형태를 모두 "존재"시킨다.
        _fake_listing(monkeypatch, tmp_dir, [NFC_NAME, NFD_NAME])
        assert resolve_normalized(tmp_dir / NFD_NAME) == tmp_dir / NFD_NAME
        assert resolve_normalized(tmp_dir / NFC_NAME) == tmp_dir / NFC_NAME

    def test_ambiguous_match_without_exact_name_is_deterministic(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # U+212B(옹스트롬 기호)와 A+U+030A는 NFC가 모두 U+00C5라서 입력과 정확히 같은 이름이 없다.
        angstrom, decomposed, wanted = "\u212b.md", "A\u030a.md", "\u00c5.md"
        for listing in ([angstrom, decomposed], [decomposed, angstrom]):
            _fake_listing(monkeypatch, tmp_dir, listing)
            # 정렬 순서로 첫 이름을 고른다(목록 순서와 무관)
            assert resolve_normalized(tmp_dir / wanted) == tmp_dir / decomposed

    def test_many_nfc_lookups_over_nfd_names_stay_fast(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        names = [unicodedata.normalize("NFD", f"파일-{i}.md") for i in range(5000)]
        _fake_listing(monkeypatch, tmp_dir, names)
        cache = DirCache()
        started = time.perf_counter()
        for i in range(5000):
            assert resolve_normalized(tmp_dir / f"파일-{i}.md", cache) is not None
        assert resolve_normalized(tmp_dir / "없음.md", cache) is None
        assert time.perf_counter() - started < 0.5

    @pytest.mark.skipif(IS_ROOT, reason="root는 권한 검사를 우회한다")
    def test_traversable_but_unreadable_directory_falls_back_to_exact_name(
        self, tmp_dir: Path
    ) -> None:
        write_tree(tmp_dir, {"locked/f.md": "v"})
        locked = tmp_dir / "locked"
        locked.chmod(0o111)
        try:
            assert resolve_normalized(locked / "f.md") == locked / "f.md"
            assert resolve_normalized(locked / "nope.md") is None
        finally:
            locked.chmod(0o755)

    def test_dir_cache_avoids_rereading_directories(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_tree(tmp_dir, {"d/a.md": "", "d/b.md": ""})
        reads: list[str] = []
        real_listdir = os.listdir

        def spy(path: str | os.PathLike[str]) -> list[str]:
            reads.append(os.fspath(path))
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", spy)
        cache = DirCache()
        for name in ("a.md", "b.md", "a.md", "missing.md"):
            resolve_normalized(tmp_dir / "d" / name, cache)
        assert reads.count(str(tmp_dir / "d")) == 1
        assert reads.count(str(tmp_dir)) == 1

        reads.clear()
        resolve_normalized(tmp_dir / "d" / "a.md")
        resolve_normalized(tmp_dir / "d" / "b.md")
        assert reads.count(str(tmp_dir / "d")) == 2

    def test_dir_cache_listdir_returns_none_for_unreadable(self, tmp_dir: Path) -> None:
        cache = DirCache()
        assert cache.listdir(tmp_dir / "nope") is None
        assert cache.listdir(tmp_dir) == []


class TestListMarkdownFiles:
    def test_sorted_md_only_skipping_dot_folders(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"b.md": "", "a/c.md": "", "a/d.txt": "", ".hidden/e.md": ""})
        assert list_markdown_files(tmp_dir) == [tmp_dir / "a" / "c.md", tmp_dir / "b.md"]

    def test_missing_directory_is_empty(self, tmp_dir: Path) -> None:
        assert list_markdown_files(tmp_dir / "none") == []

    def test_relative_input_gives_absolute_paths(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_tree(tmp_dir, {"w/a.md": ""})
        monkeypatch.chdir(tmp_dir)
        assert list_markdown_files(Path("w")) == [tmp_dir / "w" / "a.md"]

    def test_directory_named_like_markdown_is_descended(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"x.md/inner.md": ""})
        assert list_markdown_files(tmp_dir) == [tmp_dir / "x.md" / "inner.md"]

    def test_skips_dangling_symlink(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"ok.md": ""})
        (tmp_dir / "dangling.md").symlink_to(tmp_dir / "missing-target.md")
        assert list_markdown_files(tmp_dir) == [tmp_dir / "ok.md"]

    def test_skips_file_symlink_even_when_target_exists(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/ok.md": "", "outside.md": ""})
        (tmp_dir / "wiki" / "link.md").symlink_to(tmp_dir / "outside.md")
        assert list_markdown_files(tmp_dir / "wiki") == [tmp_dir / "wiki" / "ok.md"]

    def test_does_not_follow_directory_symlink(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/ok.md": "", "elsewhere/secret.md": ""})
        (tmp_dir / "wiki" / "out").symlink_to(tmp_dir / "elsewhere", target_is_directory=True)
        assert list_markdown_files(tmp_dir / "wiki") == [tmp_dir / "wiki" / "ok.md"]

    def test_self_linking_directories_terminate(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/real.md": ""})
        (tmp_dir / "wiki" / "a").symlink_to(".", target_is_directory=True)
        (tmp_dir / "wiki" / "b").symlink_to(".", target_is_directory=True)
        assert list_markdown_files(tmp_dir / "wiki") == [tmp_dir / "wiki" / "real.md"]

    def test_link_pointing_at_ancestor_terminates(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/sub/real.md": ""})
        (tmp_dir / "wiki" / "sub" / "up").symlink_to("../..", target_is_directory=True)
        assert list_markdown_files(tmp_dir / "wiki") == [tmp_dir / "wiki" / "sub" / "real.md"]

    def test_root_given_as_symlink_is_followed(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"real/a.md": ""})
        (tmp_dir / "alias").symlink_to(tmp_dir / "real", target_is_directory=True)
        assert list_markdown_files(tmp_dir / "alias") == [tmp_dir / "alias" / "a.md"]

    def test_deeply_nested_tree_does_not_recurse(self, tmp_dir: Path) -> None:
        deep = tmp_dir.joinpath(*["d"] * 60)
        deep.mkdir(parents=True)
        (deep / "leaf.md").write_text("", encoding="utf-8")
        assert list_markdown_files(tmp_dir) == [deep / "leaf.md"]

    @pytest.mark.skipif(IS_ROOT, reason="root는 권한 검사를 우회한다")
    def test_skips_unreadable_subdirectory(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"ok.md": "", "locked/hidden.md": ""})
        locked = tmp_dir / "locked"
        locked.chmod(0o000)
        try:
            assert list_markdown_files(tmp_dir) == [tmp_dir / "ok.md"]
        finally:
            locked.chmod(0o755)

    def test_skips_entries_that_vanish_during_scan(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_tree(tmp_dir, {"a.md": "", "gone.md": ""})
        real_scandir = os.scandir

        class _Entries:
            def __init__(self, path: Path) -> None:
                self._it = real_scandir(path)

            def __enter__(self) -> Iterator[os.DirEntry[str]]:
                return self._gen()

            def _gen(self) -> Iterator[os.DirEntry[str]]:
                for entry in self._it:
                    if entry.name == "gone.md":
                        os.unlink(entry.path)  # 스캔 도중 사라진 항목
                    yield entry

            def __exit__(self, *exc: object) -> None:
                self._it.close()

        monkeypatch.setattr(os, "scandir", lambda p: _Entries(Path(p)))
        result = list_markdown_files(tmp_dir)
        assert tmp_dir / "a.md" in result
        assert tmp_dir / "gone.md" not in result


class TestFindSymlinks:
    def test_finds_file_directory_and_dangling_links_sorted(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/real.md": "", "wiki/sub/x.md": "", "other/o.md": ""})
        (tmp_dir / "wiki" / "z-file.md").symlink_to(tmp_dir / "other" / "o.md")
        (tmp_dir / "wiki" / "a-dir").symlink_to(tmp_dir / "other", target_is_directory=True)
        (tmp_dir / "wiki" / "sub" / "dangling").symlink_to(tmp_dir / "nowhere")
        assert find_symlinks(tmp_dir / "wiki") == [
            tmp_dir / "wiki" / "a-dir",
            tmp_dir / "wiki" / "sub" / "dangling",
            tmp_dir / "wiki" / "z-file.md",
        ]

    def test_does_not_follow_links_and_terminates_on_loops(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/real.md": ""})
        (tmp_dir / "wiki" / "a").symlink_to(".", target_is_directory=True)
        (tmp_dir / "wiki" / "b").symlink_to(".", target_is_directory=True)
        assert find_symlinks(tmp_dir / "wiki") == [tmp_dir / "wiki" / "a", tmp_dir / "wiki" / "b"]

    def test_includes_links_inside_dot_folders(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/.cache/f.txt": ""})
        link = tmp_dir / "wiki" / ".cache" / "l"
        link.symlink_to(tmp_dir / "wiki")
        assert find_symlinks(tmp_dir / "wiki") == [link]

    def test_no_links_or_missing_directory_is_empty(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"a.md": ""})
        assert find_symlinks(tmp_dir) == []
        assert find_symlinks(tmp_dir / "none") == []


class TestWriteFileAtomic:
    def test_creates_directories_and_writes(self, tmp_dir: Path) -> None:
        file = tmp_dir / "x" / "y.json"
        write_file_atomic(file, "{}\n")
        assert file.read_text(encoding="utf-8") == "{}\n"

    def test_overwrites_existing_and_leaves_only_the_target(self, tmp_dir: Path) -> None:
        file = tmp_dir / "f.md"
        file.write_text("old", encoding="utf-8")
        write_file_atomic(file, "새 내용")
        assert file.read_text(encoding="utf-8") == "새 내용"
        assert [p.name for p in tmp_dir.iterdir()] == ["f.md"]

    def test_does_not_translate_newlines(self, tmp_dir: Path) -> None:
        file = tmp_dir / "f.md"
        write_file_atomic(file, "a\r\nb\n")
        assert file.read_bytes() == b"a\r\nb\n"

    def test_rename_failure_removes_temp_file_and_propagates(self, tmp_dir: Path) -> None:
        target = tmp_dir / "target"
        target.mkdir()
        (target / "keep.txt").write_text("k", encoding="utf-8")
        with pytest.raises(OSError):
            write_file_atomic(target, "content")
        assert [p.name for p in tmp_dir.iterdir()] == ["target"]
        assert (target / "keep.txt").read_text(encoding="utf-8") == "k"

    def test_replace_failure_keeps_original_and_cleans_up(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        file = tmp_dir / "f.md"
        file.write_text("original", encoding="utf-8")

        def boom(src: object, dst: object) -> None:
            raise OSError("replace 실패")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="replace 실패"):
            write_file_atomic(file, "new")
        monkeypatch.undo()
        assert file.read_text(encoding="utf-8") == "original"
        assert [p.name for p in tmp_dir.iterdir()] == ["f.md"]

    def test_write_failure_removes_partial_temp_file(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def partial(self: Path, data: bytes) -> int:
            with self.open("wb") as handle:
                handle.write(data[:2])
            raise OSError("디스크가 가득 찼습니다")

        monkeypatch.setattr(Path, "write_bytes", partial)
        with pytest.raises(OSError, match="디스크"):
            write_file_atomic(tmp_dir / "f.md", "content")
        monkeypatch.undo()
        assert list(tmp_dir.iterdir()) == []

    def test_keyboard_interrupt_also_cleans_up_and_propagates(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupt(src: object, dst: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            write_file_atomic(tmp_dir / "f.md", "content")
        monkeypatch.undo()
        assert list(tmp_dir.iterdir()) == []

    def test_temp_names_are_unique_and_in_same_directory(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sources: list[Path] = []
        real_replace = os.replace

        def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            sources.append(Path(src))
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", spy)
        file = tmp_dir / "sub" / "f.md"
        write_file_atomic(file, "1")
        write_file_atomic(file, "2")
        assert len(set(sources)) == 2
        assert all(s.parent == file.parent for s in sources)
        assert all(str(os.getpid()) in s.name for s in sources)

    def test_long_file_name_still_works(self, tmp_dir: Path) -> None:
        file = tmp_dir / ("a" * 217 + ".md")
        write_file_atomic(file, "x")
        assert file.read_text(encoding="utf-8") == "x"


class TestReadTextIfExists:
    def test_missing_is_empty(self, tmp_dir: Path) -> None:
        assert read_text_if_exists(tmp_dir / "none.md") == ""

    def test_reads_utf8(self, tmp_dir: Path) -> None:
        file = tmp_dir / "a.md"
        file.write_text("안녕\n", encoding="utf-8")
        assert read_text_if_exists(file) == "안녕\n"

    def test_empty_file(self, tmp_dir: Path) -> None:
        file = tmp_dir / "e.md"
        file.write_bytes(b"")
        assert read_text_if_exists(file) == ""

    def test_directory_is_empty(self, tmp_dir: Path) -> None:
        (tmp_dir / "d").mkdir()
        assert read_text_if_exists(tmp_dir / "d") == ""

    def test_symlink_is_empty(self, tmp_dir: Path) -> None:
        (tmp_dir / "target.md").write_text("내용", encoding="utf-8")
        (tmp_dir / "link.md").symlink_to(tmp_dir / "target.md")
        assert read_text_if_exists(tmp_dir / "link.md") == ""
        assert read_text_if_exists(tmp_dir / "target.md") == "내용"

    def test_broken_symlink_is_empty(self, tmp_dir: Path) -> None:
        (tmp_dir / "link.md").symlink_to(tmp_dir / "none.md")
        assert read_text_if_exists(tmp_dir / "link.md") == ""

    def test_only_first_max_bytes_are_read(self, tmp_dir: Path) -> None:
        file = tmp_dir / "big.md"
        with file.open("wb") as handle:
            handle.write(b"a" * 4_194_304)
            handle.write(b"b" * 1000)
        text = read_text_if_exists(file)
        assert len(text) == 4_194_304
        assert set(text) == {"a"}

    def test_custom_max_bytes(self, tmp_dir: Path) -> None:
        file = tmp_dir / "a.md"
        file.write_text("abcdef", encoding="utf-8")
        assert read_text_if_exists(file, max_bytes=3) == "abc"
        assert read_text_if_exists(file, max_bytes=0) == ""

    def test_truncation_in_the_middle_of_a_character_does_not_raise(self, tmp_dir: Path) -> None:
        file = tmp_dir / "k.md"
        file.write_text("가나다", encoding="utf-8")  # 글자마다 3바이트
        assert read_text_if_exists(file, max_bytes=4) == "가\ufffd"

    def test_invalid_utf8_is_replaced(self, tmp_dir: Path) -> None:
        file = tmp_dir / "bad.md"
        file.write_bytes(b"ok \xff\xfe end")
        assert read_text_if_exists(file) == "ok \ufffd\ufffd end"

    def test_bom_is_removed(self, tmp_dir: Path) -> None:
        file = tmp_dir / "bom.md"
        file.write_bytes(b"\xef\xbb\xbf# \xec\xa0\x9c\xeb\xaa\xa9")
        assert read_text_if_exists(file) == "# 제목"

    def test_bom_only_at_start(self, tmp_dir: Path) -> None:
        file = tmp_dir / "bom2.md"
        file.write_bytes(b"a\xef\xbb\xbfb")
        assert read_text_if_exists(file) == "a\ufeffb"

    def test_fifo_does_not_block(self, tmp_dir: Path) -> None:
        fifo = tmp_dir / "pipe"
        os.mkfifo(fifo)
        assert read_text_if_exists(fifo) == ""

    def test_nul_in_path_does_not_raise(self, tmp_dir: Path) -> None:
        assert read_text_if_exists(Path(str(tmp_dir / "a") + "\x00b")) == ""

    def test_does_not_leak_file_descriptors(self, tmp_dir: Path) -> None:
        (tmp_dir / "d").mkdir()
        os.mkfifo(tmp_dir / "pipe")
        (tmp_dir / "t.md").write_text("x", encoding="utf-8")
        (tmp_dir / "link.md").symlink_to(tmp_dir / "t.md")
        targets = [tmp_dir / "d", tmp_dir / "pipe", tmp_dir / "link.md", tmp_dir / "none.md"]
        targets.append(tmp_dir / "t.md")
        for target in targets:  # 한 번씩 돌려 지연 초기화를 끝낸다
            read_text_if_exists(target)
        before = open_fd_count()
        with hang_guard():
            for _ in range(200):
                for target in targets:
                    read_text_if_exists(target)
        assert open_fd_count() == before

    def test_huge_max_bytes_does_not_allocate_that_much(self, tmp_dir: Path) -> None:
        file = tmp_dir / "small.md"
        file.write_text("작은 파일", encoding="utf-8")
        assert read_text_if_exists(file, max_bytes=2**62) == "작은 파일"
        assert read_text_if_exists(file, max_bytes=2**100) == "작은 파일"

    def test_fifo_with_no_writer_does_not_hang(self, tmp_dir: Path) -> None:
        fifo = tmp_dir / "pipe"
        os.mkfifo(fifo)
        with hang_guard():
            assert read_text_if_exists(fifo) == ""
