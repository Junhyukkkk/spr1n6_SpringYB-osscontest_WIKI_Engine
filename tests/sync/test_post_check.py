"""사후 검사·쓰기 루트 사전 검사. 보안 경계라서 적대적 입력을 많이 넣는다."""

import os
import unicodedata
from pathlib import Path

import pytest

from team_wiki_engine.git.client import GitClient
from team_wiki_engine.git.ops import CloneStateError, changed_paths, parse_porcelain_z
from team_wiki_engine.sync import post_check
from team_wiki_engine.sync.post_check import (
    PathCheck,
    check_changed_paths,
    check_write_root,
    verify_claude_changes,
)
from tests.helpers import write_tree
from tests.helpers.git import init_repo


class TestCheckChangedPaths:
    def test_wiki_아래만_허용하고_log_md는_막는다(self) -> None:
        assert check_changed_paths(["wiki/a.md", "wiki/status/w.md"]) == PathCheck(True, ())
        result = check_changed_paths(
            ["wiki/a.md", "raw/x.md", "baseline.json", "wiki/log.md", "metrics/w.md"]
        )
        assert result == PathCheck(
            False, ("raw/x.md", "baseline.json", "wiki/log.md", "metrics/w.md")
        )

    def test_빈_입력은_통과한다(self) -> None:
        assert check_changed_paths([]) == PathCheck(True, ())

    def test_NFD로_들어온_경로도_같은_규칙을_쓴다(self) -> None:
        # Review Focus 4
        assert check_changed_paths([unicodedata.normalize("NFD", "wiki/결정.md")]).ok
        assert not check_changed_paths([unicodedata.normalize("NFD", "원본/결정.md")]).ok
        assert check_changed_paths([unicodedata.normalize("NFD", "wiki/새 이름.md")]).ok

    def test_NFD로_쓴_wiki_log_md_변형도_막는다(self) -> None:
        assert not check_changed_paths([unicodedata.normalize("NFD", "wiki/log.md")]).ok

    @pytest.mark.parametrize(
        "path",
        [
            "wiki/LOG.md",
            "wiki/Log.md",
            "wiki/LOG.MD",
            "wiki/lOg.Md",
            "wiki/log.md/",
            "wiki/log.md.",  # 윈도·SMB가 같은 파일로 보는 꼴
            "wiki/log.md ",
            "wiki/./log.md",
            "wiki//log.md",
            "wiki/x/../log.md",
            "wiki/log.md\x00",
            "wiki/log.md​",  # 서식 문자
            "wiki/lo\u0000g.md",
            "wi\u212ai/log.md",  # 켈빈 기호는 NFC에서 대문자 K가 돼 접두어도 어긋난다
        ],
    )
    def test_log_md_변형은_모두_위반(self, path: str) -> None:
        assert not check_changed_paths([path]).ok

    @pytest.mark.parametrize(
        "path",
        [
            "WIKI/a.md",
            "Wiki/a.md",
            "wiki",  # wiki 자체
            "wiki/",  # 폴더 자체의 항목은 wiki/ 밑이 아니다
            "",
            "/wiki/a.md",
            "./wiki/a.md",
            "wiki/../raw/a.md",
            "wiki/a/../../raw/a.md",
            "wikis/a.md",
            "baseline.json",
            ".git/config",
            "wiki/.git/config",
            "wiki/.GIT/config",
            "wiki/.Git",
            "wiki/a/.git/hooks/pre-commit",
            "wiki/.git ",
            "wiki/.git.",
            "wiki/git~1/x",
            "wiki/GIT~1",
            "wiki/．git/x",  # 전각 마침표
            "wiki/.gitattributes",
            "wiki/a/.gitignore",
            "wiki/.gitmodules",
            "wiki/\udcff.md",
            "wiki/\ud800.md",
            "wiki/a\nb.md",
            "wiki/a\rb.md",
            "wiki/a\tb.md",
            "wiki/a\x1bb.md",
            "wiki/a\x85b.md",
            "wiki/a b.md",
            "wiki/a b.md",
            "wiki/‮b.md",
            "wiki/a​b.md",
            "wiki/a﻿b.md",
            "wiki/ab.md",  # 사설 영역
            "wiki/a\x7fb.md",
        ],
    )
    def test_위반_경로(self, path: str) -> None:
        result = check_changed_paths([path])
        assert result.ok is False
        assert len(result.offending) == 1

    def test_허용_경로(self) -> None:
        paths = [
            "wiki/새 이름.md",
            "wiki/a b/c.md",
            "wiki/.hidden.md",
            "wiki/status/x.md",
            "wiki/logs.md",
            "wiki/sub/log.md",  # 최상위 log.md만 스크립트 전용
            "wiki/log.md.bak",
            "wiki/log.mdx",
            "wiki/mylog.md",
            "wiki/ｌｏｇ.ｍｄ",  # 전각은 다른 파일이다
            "wiki/\u029fog.md",  # 작은 대문자 L은 casefold로도 log가 아니다
            "wiki/\u0130.md",  # 점 있는 İ
            "wiki/\ufb01le.md",  # fi 합자
            "wiki/stra\u00dfe.md",  # ß
            "wiki/dir/",  # 무시된 폴더 항목
            "wiki/.github.md",
            "wiki/gitignore.md",
            "wiki/a\\b.md",
        ]
        assert check_changed_paths(paths) == PathCheck(True, ())

    def test_순수_함수에서_요구한_예시(self) -> None:
        bad = [
            "wiki/LOG.md",
            "WIKI/a.md",
            "wiki/.git/config",
            "wiki/\udcff.md",
            "wiki/a\nb.md",
            "wiki/‮b.md",
        ]
        result = check_changed_paths(bad)
        assert not result.ok
        assert len(result.offending) == len(bad)
        assert check_changed_paths([unicodedata.normalize("NFD", "wiki/새 이름.md")]).ok

    def test_표시용_경로는_위조_문자와_대리_문자를_가린다(self) -> None:
        result = check_changed_paths(["wiki/\udcff.md", "raw/a\nb.md", "raw/‮b.md"])
        assert result.offending == ("wiki/\\xff.md", "raw/a\\nb.md", "raw/\\u202eb.md")
        for shown in result.offending:
            assert shown.isprintable()
            shown.encode("utf-8")  # 대리 문자가 남아 있지 않다

    def test_고립_대리_문자도_표시용으로_바꾼다(self) -> None:
        result = check_changed_paths(["raw/\ud800x\udfff.md"])
        assert result.offending == ("raw/\\ud800x\\udfff.md",)
        result.offending[0].encode("utf-8")

    def test_비밀값처럼_보이는_이름은_표시에서_가린다(self) -> None:
        token = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"
        result = check_changed_paths([f"raw/{token}.txt"])
        assert not result.ok
        assert token not in "".join(result.offending)
        assert result.offending[0].startswith("raw/")

    def test_같은_경로는_한_번만(self) -> None:
        result = check_changed_paths(["raw/x.md", "wiki/a.md", "raw/x.md", "raw/x.md"])
        assert result.offending == ("raw/x.md",)

    def test_NFC_NFD_두_꼴은_따로_센다(self) -> None:
        nfc = "raw/결정.md"
        nfd = unicodedata.normalize("NFD", nfc)
        result = check_changed_paths([nfc, nfd])
        assert len(result.offending) == 2  # 입력이 다르면 표시도 다르다(원본 기준 중복 제거)

    def test_긴_경로도_표시는_자른다(self) -> None:
        result = check_changed_paths(["raw/" + "가" * 5000])
        assert not result.ok
        assert len(result.offending[0]) < 1000

    @pytest.mark.parametrize(
        "bad_input",
        [
            "wiki/a.md",  # 문자열 하나
            b"wiki/a.md",
            None,
            [None],
            [1, 2],
            [b"wiki/a.md"],
            [("wiki/a.md",)],
            [object()],
            ["wiki/a.md", None],
            42,
        ],
    )
    def test_어떤_입력에도_예외를_던지지_않는다(self, bad_input: object) -> None:
        result = check_changed_paths(bad_input)  # type: ignore[arg-type]
        assert isinstance(result, PathCheck)
        if bad_input == "wiki/a.md":
            assert result.ok
        else:
            assert not result.ok
            assert result.offending

    def test_반복_도중_예외가_나는_입력도_위반(self) -> None:
        def boom() -> object:
            yield "wiki/a.md"
            raise RuntimeError("boom")

        result = check_changed_paths(boom())  # type: ignore[arg-type]
        assert not result.ok

    def test_거대한_목록도_끝난다(self) -> None:
        result = check_changed_paths([f"wiki/n/{i}.md" for i in range(20000)])
        assert result.ok

    def test_결과는_불변(self) -> None:
        result = check_changed_paths(["raw/x"])
        with pytest.raises(AttributeError):
            result.ok = True  # type: ignore[misc]


class TestRedactBeforeTruncate:
    OPAQUE = "correcthorsebatterystaple"

    @pytest.mark.parametrize("pad", range(270, 303))
    def test_300자_경계에_걸친_알려진_비밀값은_조각도_남지_않는다(self, pad: int) -> None:
        long_dir = "d" * 100
        # `raw/` + 100 + `/` + 100 + `/` = 206자, 거기서 `pad - 206`자 뒤에 비밀값이 시작한다
        path = f"raw/{long_dir}/{long_dir}/{'e' * (pad - 206)}{self.OPAQUE}.md"
        result = check_changed_paths([path], (self.OPAQUE,))
        assert not result.ok
        shown = "\n".join(result.offending)
        assert "correcth" not in shown, pad
        assert self.OPAQUE not in shown, pad

    async def test_verify_claude_changes에도_redact를_넘긴다(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir / "data", {"wiki/a.md": "a"})
        long_dir = "d" * 100
        name = f"raw/{long_dir}/{long_dir}/{'e' * 80}{self.OPAQUE}.md"
        write_tree(git.cwd, {name: "x"})
        result = await verify_claude_changes(git, (self.OPAQUE,))
        assert not result.ok
        assert "correcth" not in "\n".join(result.offending)

    def test_쓰기_루트_사유의_링크_이름도_가린다(self, tmp_dir: Path) -> None:
        wiki = tmp_dir / "wiki"
        wiki.mkdir()
        long_dir = "d" * 100
        name = f"{long_dir}/{long_dir}/{'e' * 80}{self.OPAQUE}"
        (wiki / long_dir / long_dir).mkdir(parents=True)
        os.symlink(tmp_dir, wiki / name)
        reason = check_write_root(tmp_dir, (self.OPAQUE,))
        assert reason is not None
        assert "correcth" not in reason


class TestVerifyClaudeChanges:
    async def test_위반이면_작업_사본을_되돌린다(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a", "raw/r.md": "r"})
        write_tree(tmp_dir, {"wiki/a.md": "changed", "raw/x.md": "x"})
        result = await verify_claude_changes(git)
        assert result == PathCheck(False, ("raw/x.md",))
        assert await changed_paths(git) == []
        assert (tmp_dir / "wiki/a.md").read_text(encoding="utf-8") == "a"
        assert not (tmp_dir / "raw/x.md").exists()

    async def test_허용_범위면_그대로_둔다(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a"})
        write_tree(tmp_dir, {"wiki/new dir/새 파일.md": "n"})
        assert await verify_claude_changes(git) == PathCheck(True, ())
        assert await changed_paths(git) == ["wiki/new dir/새 파일.md"]
        assert (tmp_dir / "wiki/new dir/새 파일.md").read_text(encoding="utf-8") == "n"

    async def test_바뀐_게_없으면_통과(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a"})
        assert await verify_claude_changes(git) == PathCheck(True, ())

    async def test_이름_바꾸기는_양쪽_경로_모두_검사한다(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a"})
        (tmp_dir / "raw").mkdir()
        await git.run(["mv", "wiki/a.md", "raw/a.md"])
        result = await verify_claude_changes(git)
        assert result.ok is False
        assert "raw/a.md" in result.offending
        assert (tmp_dir / "wiki/a.md").read_text(encoding="utf-8") == "a"
        assert not (tmp_dir / "raw/a.md").exists()
        assert await changed_paths(git) == []

    async def test_wiki_밖에서_wiki_안으로_옮겨도_옛_경로가_위반(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"raw/a.md": "a", "wiki/index.md": "i"})
        await git.run(["mv", "raw/a.md", "wiki/a.md"])
        result = await verify_claude_changes(git)
        assert result == PathCheck(False, ("raw/a.md",)) or "raw/a.md" in result.offending
        assert (tmp_dir / "raw/a.md").exists()
        assert not (tmp_dir / "wiki/a.md").exists()

    async def test_baseline_json_삭제는_위반(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a", "baseline.json": "{}\n"})
        (tmp_dir / "baseline.json").unlink()
        result = await verify_claude_changes(git)
        assert result == PathCheck(False, ("baseline.json",))
        assert (tmp_dir / "baseline.json").read_text(encoding="utf-8") == "{}\n"

    @pytest.mark.parametrize("action", ["modify", "delete"])
    async def test_기존_wiki_log_md_수정과_삭제는_위반(self, tmp_dir: Path, action: str) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a", "wiki/log.md": "# log\n"})
        if action == "modify":
            (tmp_dir / "wiki/log.md").write_text("# hacked\n", encoding="utf-8")
        else:
            (tmp_dir / "wiki/log.md").unlink()
        result = await verify_claude_changes(git)
        assert result == PathCheck(False, ("wiki/log.md",))
        assert (tmp_dir / "wiki/log.md").read_text(encoding="utf-8") == "# log\n"

    async def test_gitignore에_걸린_파일도_위반이고_되돌린_뒤_사라진다(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a", ".gitignore": "raw/secret.txt\n"})
        write_tree(tmp_dir, {"raw/secret.txt": "s"})
        assert (tmp_dir / "raw/secret.txt").exists()
        result = await verify_claude_changes(git)
        assert result.ok is False
        assert any(p.startswith("raw/") for p in result.offending)
        assert not (tmp_dir / "raw/secret.txt").exists()

    async def test_wiki_안의_숨김_파일은_허용(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a"})
        write_tree(tmp_dir, {"wiki/.DS_Store": "x"})
        assert (await verify_claude_changes(git)).ok

    async def test_wiki_안의_gitignore된_파일은_wiki_아래라서_허용(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a", ".gitignore": "wiki/cache/\n"})
        write_tree(tmp_dir, {"wiki/cache/x.md": "x"})
        assert "wiki/cache/" in await changed_paths(git)  # 무시된 폴더도 목록에 오른다
        assert (await verify_claude_changes(git)).ok

    async def test_wiki_밑의_git_설정_파일은_위반이고_지워진다(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a"})
        write_tree(tmp_dir, {"wiki/sub/.gitattributes": "* -text\n", "wiki/b.md": "b"})
        result = await verify_claude_changes(git)
        assert result == PathCheck(False, ("wiki/sub/.gitattributes",))
        assert not (tmp_dir / "wiki/sub").exists()
        assert not (tmp_dir / "wiki/b.md").exists()

    async def test_잘못된_UTF8_이름은_위반이고_지워진다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a"})
        write_tree(tmp_dir, {"wiki/junk.md": "x"})

        async def fake_changed(_git: GitClient) -> list[str]:
            return parse_porcelain_z(b"?? wiki/\xff.md\0")  # 대리 문자가 든 경로

        monkeypatch.setattr(post_check, "changed_paths", fake_changed)
        result = await verify_claude_changes(git)
        assert result == PathCheck(False, ("wiki/\\xff.md",))
        assert not (tmp_dir / "wiki/junk.md").exists()  # 되돌렸다

    async def test_파일_시스템이_허용하면_잘못된_UTF8_파일도_위반(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a"})
        bad = os.path.join(os.fsencode(tmp_dir), b"wiki", b"\xff.md")
        try:
            with open(bad, "wb") as handle:
                handle.write(b"x")
        except OSError:
            pytest.skip("이 파일 시스템은 잘못된 UTF-8 이름을 거부한다")
        result = await verify_claude_changes(git)
        assert result.offending == ("wiki/\\xff.md",)
        assert not os.path.exists(bad)

    async def test_줄바꿈_이름은_위반(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir, {"wiki/a.md": "a"})
        write_tree(tmp_dir, {"wiki/a\nb.md": "x"})
        result = await verify_claude_changes(git)
        assert result.offending == ("wiki/a\\nb.md",)
        assert not (tmp_dir / "wiki/a\nb.md").exists()

    async def test_작업_사본이_아니면_예외가_그대로_난다(self, tmp_dir: Path) -> None:
        with pytest.raises(CloneStateError):
            await verify_claude_changes(GitClient(tmp_dir, allowed_protocols="file"))


class TestCheckWriteRoot:
    def test_wiki가_없으면_실제_폴더를_만들고_통과한다(self, tmp_dir: Path) -> None:
        assert check_write_root(tmp_dir) is None
        assert (tmp_dir / "wiki").is_dir()
        assert not (tmp_dir / "wiki").is_symlink()

    def test_이미_있는_wiki는_그대로_통과(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/a.md": "a", "wiki/sub/b.md": "b"})
        assert check_write_root(tmp_dir) is None
        assert (tmp_dir / "wiki/a.md").read_text(encoding="utf-8") == "a"

    def test_wiki가_다른_폴더를_가리키는_링크면_사유(self, tmp_dir: Path) -> None:
        outside = tmp_dir / "outside"
        outside.mkdir()
        data = tmp_dir / "data"
        data.mkdir()
        os.symlink(outside, data / "wiki")
        assert check_write_root(data) == "wiki 폴더가 심볼릭 링크입니다"
        assert os.readlink(data / "wiki") == str(outside)
        assert list(outside.iterdir()) == []

    def test_wiki가_깨진_링크여도_따라가서_만들지_않는다(self, tmp_dir: Path) -> None:
        data = tmp_dir / "data"
        data.mkdir()
        os.symlink(tmp_dir / "nowhere", data / "wiki")
        assert check_write_root(data) == "wiki 폴더가 심볼릭 링크입니다"
        assert not (tmp_dir / "nowhere").exists()

    def test_wiki가_파일이면_사유(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki": "file"})
        assert check_write_root(tmp_dir) == "wiki가 폴더가 아닙니다"
        assert (tmp_dir / "wiki").read_text(encoding="utf-8") == "file"

    def test_깨진_링크와_폴더_링크는_이름을_사유에_싣는다(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/real/ok.md": "ok"})
        os.symlink(tmp_dir / "nowhere", tmp_dir / "wiki" / "x.md")
        os.symlink(tmp_dir, tmp_dir / "wiki" / "real" / "d")
        reason = check_write_root(tmp_dir)
        assert reason is not None
        assert reason.startswith("wiki 아래에 심볼릭 링크가 있습니다: ")
        assert "x.md" in reason
        assert "real/d" in reason

    def test_비밀값처럼_보이는_링크_이름은_가려진다(self, tmp_dir: Path) -> None:
        token = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"
        write_tree(tmp_dir, {"wiki/a.md": "a"})
        os.symlink(tmp_dir / "nowhere", tmp_dir / "wiki" / token)
        reason = check_write_root(tmp_dir)
        assert reason is not None
        assert token not in reason
        assert "<이름 생략>" in reason

    def test_제어_문자_이름은_이스케이프된다(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/a.md": "a"})
        os.symlink(tmp_dir / "nowhere", tmp_dir / "wiki" / "a\nb")
        reason = check_write_root(tmp_dir)
        assert reason is not None
        assert "\n" not in reason
        assert "a\\nb" in reason

    def test_잘못된_UTF8_이름의_링크도_예외_없이_사유(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/a.md": "a"})
        link = os.path.join(os.fsencode(tmp_dir), b"wiki", b"\xff")
        try:
            os.symlink(os.fsencode(tmp_dir / "nowhere"), link)
        except OSError:
            pytest.skip("이 파일 시스템은 잘못된 UTF-8 이름을 거부한다")
        reason = check_write_root(tmp_dir)
        assert reason is not None
        reason.encode("utf-8")
        assert "\\xff" in reason

    def test_링크는_최대_5개만_싣는다(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/a.md": "a"})
        for index in range(9):
            os.symlink(tmp_dir / "nowhere", tmp_dir / "wiki" / f"l{index}")
        reason = check_write_root(tmp_dir)
        assert reason is not None
        assert sum(f"l{i}" in reason for i in range(9)) == 5
        assert "4" in reason.rsplit("외", 1)[-1]

    def test_조상이_링크여도_data_dir이_폴더면_통과(self, tmp_dir: Path) -> None:
        real = tmp_dir / "real"
        (real / "data").mkdir(parents=True)
        os.symlink(real, tmp_dir / "alias")
        assert check_write_root(tmp_dir / "alias" / "data") is None
        assert (real / "data" / "wiki").is_dir()

    def test_data_dir_자체가_링크면_사유(self, tmp_dir: Path) -> None:
        (tmp_dir / "real").mkdir()
        os.symlink(tmp_dir / "real", tmp_dir / "data")
        reason = check_write_root(tmp_dir / "data")
        assert reason is not None
        assert not (tmp_dir / "real" / "wiki").exists()

    def test_data_dir이_파일이거나_없으면_사유(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"f": "x"})
        assert check_write_root(tmp_dir / "f") is not None
        assert check_write_root(tmp_dir / "missing") is not None
        assert not (tmp_dir / "missing").exists()

    def test_macOS_var_링크_경로(self, raw_tmp_dir: Path) -> None:
        assert check_write_root(raw_tmp_dir) is None

    def test_wiki_만들기가_막히면_사유로_돌려준다(self, tmp_dir: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root는 권한 검사를 건너뛴다")
        data = tmp_dir / "data"
        data.mkdir()
        data.chmod(0o500)
        try:
            reason = check_write_root(data)
        finally:
            data.chmod(0o700)
        assert reason is not None
        assert str(tmp_dir) not in reason

    def test_mkdir_경합에서_링크가_먼저_생기면_사유(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = tmp_dir / "data"
        data.mkdir()
        outside = tmp_dir / "outside"
        outside.mkdir()
        real_mkdir = os.mkdir

        def racing_mkdir(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
            os.symlink(outside, path)  # 검사와 mkdir 사이에 공격자가 링크를 심는다
            real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "mkdir", racing_mkdir)
        assert check_write_root(data) == "wiki 폴더가 심볼릭 링크입니다"
        assert list(outside.iterdir()) == []

    def test_mkdir_경합에서_다른_쪽이_폴더를_먼저_만들면_통과(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = tmp_dir / "data"
        data.mkdir()
        real_mkdir = os.mkdir

        def racing_mkdir(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
            real_mkdir(path)
            real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]  # FileExistsError

        monkeypatch.setattr(os, "mkdir", racing_mkdir)
        assert check_write_root(data) is None

    @pytest.mark.parametrize("bad", [None, 42, b"\xff", "a\0b", Path("a\0b")])
    def test_어떤_입력에도_예외를_던지지_않는다(self, bad: object) -> None:
        reason = check_write_root(bad)  # type: ignore[arg-type]
        assert isinstance(reason, str)

    def test_resolve를_부르지_않는다(self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: object, **kwargs: object) -> Path:
            raise AssertionError("resolve 금지")

        monkeypatch.setattr(Path, "resolve", boom)
        monkeypatch.setattr(os.path, "realpath", boom)
        assert check_write_root(tmp_dir) is None


def _outside(tmp_dir: Path) -> Path:
    outside = tmp_dir / "outside"
    write_tree(outside, {"secret.txt": "S"})
    return outside


def _assert_outside_untouched(outside: Path) -> None:
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "S"


@pytest.fixture
async def repo(tmp_dir: Path) -> tuple[GitClient, Path]:
    work = tmp_dir / "work"
    git = await init_repo(work, {"wiki/a.md": "a", "wiki/sub/b.md": "b", "raw/r.md": "r"})
    return git, work


class TestVerifyWalksWiki:
    """`git status`가 보여 주지 못하는 것(링크·중첩 저장소·읽을 수 없는 폴더)을 직접 걷는다."""

    async def test_새_링크_파일은_위반이고_지워진다(
        self, tmp_dir: Path, repo: tuple[GitClient, Path]
    ) -> None:
        git, work = repo
        outside = _outside(tmp_dir)
        os.symlink(outside / "secret.txt", work / "wiki" / "l.md")
        result = await verify_claude_changes(git)
        assert result.ok is False
        assert "wiki/l.md" in result.offending
        assert not os.path.lexists(work / "wiki" / "l.md")
        assert await changed_paths(git) == []
        _assert_outside_untouched(outside)

    async def test_폴더_링크는_위반이고_지워진다(
        self, tmp_dir: Path, repo: tuple[GitClient, Path]
    ) -> None:
        git, work = repo
        outside = _outside(tmp_dir)
        os.symlink(outside, work / "wiki" / "sub" / "d")
        result = await verify_claude_changes(git)
        assert result.ok is False
        assert "wiki/sub/d" in result.offending
        assert not os.path.lexists(work / "wiki" / "sub" / "d")
        _assert_outside_untouched(outside)

    @pytest.mark.parametrize("symlinks_off", [False, True])
    async def test_파일을_링크로_바꾸면_위반이고_원래_파일로_돌아온다(
        self, tmp_dir: Path, repo: tuple[GitClient, Path], symlinks_off: bool
    ) -> None:
        git, work = repo
        if symlinks_off:
            await git.run(["config", "core.symlinks", "false"])
        outside = _outside(tmp_dir)
        (work / "wiki" / "a.md").unlink()
        os.symlink(outside / "secret.txt", work / "wiki" / "a.md")
        result = await verify_claude_changes(git)
        assert result.ok is False
        assert "wiki/a.md" in result.offending
        assert not (work / "wiki" / "a.md").is_symlink()
        assert (work / "wiki" / "a.md").read_text(encoding="utf-8") == "a"
        assert await changed_paths(git) == []
        _assert_outside_untouched(outside)

    async def test_중첩_저장소는_위반이고_지워진다(
        self, tmp_dir: Path, repo: tuple[GitClient, Path]
    ) -> None:
        git, work = repo
        (work / "wiki" / "x").mkdir()
        await GitClient(work / "wiki" / "x", allowed_protocols="file").run(["init", "-q"])
        write_tree(work, {"wiki/x/p.md": "p"})
        result = await verify_claude_changes(git)
        assert result.ok is False
        assert "wiki/x/.git" in result.offending
        assert not (work / "wiki" / "x").exists()

    async def test_대소문자가_다른_중첩_git_폴더도_위반(
        self, tmp_dir: Path, repo: tuple[GitClient, Path]
    ) -> None:
        git, work = repo
        write_tree(work, {"wiki/x/.GiT/config": "c", "wiki/y/.git": "gitdir: /elsewhere\n"})
        result = await verify_claude_changes(git)
        assert result.ok is False
        assert "wiki/x/.GiT" in result.offending
        assert "wiki/y/.git" in result.offending

    async def test_읽을_수_없는_폴더는_위반이고_정리된다(
        self, tmp_dir: Path, repo: tuple[GitClient, Path]
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root는 권한 검사를 건너뛴다")
        git, work = repo
        write_tree(work, {"wiki/hidden/n.md": "n"})
        os.chmod(work / "wiki" / "hidden", 0o000)
        try:
            result = await verify_claude_changes(git)
        finally:
            if (work / "wiki" / "hidden").exists():
                os.chmod(work / "wiki" / "hidden", 0o700)
        assert result.ok is False
        assert "wiki/hidden" in result.offending
        assert not (work / "wiki" / "hidden").exists()

    async def test_읽을_수_없는_폴더_안의_링크도_놓치지_않는다(
        self, tmp_dir: Path, repo: tuple[GitClient, Path]
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root는 권한 검사를 건너뛴다")
        git, work = repo
        outside = _outside(tmp_dir)
        (work / "wiki" / "hidden").mkdir()
        os.symlink(outside, work / "wiki" / "hidden" / "esc")
        os.chmod(work / "wiki" / "hidden", 0o300)
        try:
            result = await verify_claude_changes(git)
        finally:
            if (work / "wiki" / "hidden").exists():
                os.chmod(work / "wiki" / "hidden", 0o700)
        assert result.ok is False
        assert "wiki/hidden" in result.offending
        _assert_outside_untouched(outside)

    async def test_wiki_폴더_자체가_링크로_바뀌어도_위반(
        self, tmp_dir: Path, repo: tuple[GitClient, Path]
    ) -> None:
        git, work = repo
        outside = _outside(tmp_dir)
        import shutil

        shutil.rmtree(work / "wiki")
        os.symlink(outside, work / "wiki")
        result = await verify_claude_changes(git)
        assert result.ok is False
        assert "wiki" in result.offending
        assert (work / "wiki" / "a.md").read_text(encoding="utf-8") == "a"
        _assert_outside_untouched(outside)

    async def test_표시_경로는_가려진다(self, tmp_dir: Path, repo: tuple[GitClient, Path]) -> None:
        git, work = repo
        token = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"
        os.symlink(tmp_dir, work / "wiki" / token)
        os.symlink(tmp_dir, work / "wiki" / "a\nb")
        result = await verify_claude_changes(git)
        joined = "".join(result.offending)
        assert token not in joined
        assert "\n" not in joined
        assert "wiki/<이름 생략>" in result.offending
        assert "wiki/a\\nb" in result.offending

    async def test_wiki가_없어도_통과한다(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir / "w2", {"raw/r.md": "r"})
        assert (await verify_claude_changes(git)).ok


class TestOffendingCap:
    def test_위반이_많으면_앞_50개와_나머지_개수를_돌려준다(self) -> None:
        import time

        paths = [f"raw/{i}.md" for i in range(100_000)]
        started = time.perf_counter()
        result = check_changed_paths(paths)
        assert time.perf_counter() - started < 2.0
        assert result.ok is False
        assert len(result.offending) == 51
        assert result.offending[:50] == tuple(f"raw/{i}.md" for i in range(50))
        assert result.offending[50] == "외 99950개"

    def test_정확히_50개면_표시만_한다(self) -> None:
        result = check_changed_paths([f"raw/{i}.md" for i in range(50)])
        assert len(result.offending) == 50
        assert not any(item.startswith("외 ") for item in result.offending)

    def test_중복은_세지_않는다(self) -> None:
        result = check_changed_paths(["raw/a.md"] * 1000 + ["raw/b.md"])
        assert result.offending == ("raw/a.md", "raw/b.md")

    def test_허용_경로가_섞여_있어도_센다(self) -> None:
        paths = [f"wiki/{i}.md" for i in range(1000)] + [f"raw/{i}.md" for i in range(60)]
        result = check_changed_paths(paths)
        assert result.offending[-1] == "외 10개"


class TestInvisibleFillers:
    @pytest.mark.parametrize("char", ["͏", "ᅟ", "ᅠ", "ㅤ", "ﾠ", "⠀", "឴", "᠋"])
    def test_보이지_않는_채움_문자는_위반(self, char: str) -> None:
        assert not check_changed_paths([f"wiki/a{char}b.md"]).ok

    @pytest.mark.parametrize(
        "name", ["가나다.md", "hello-world_1.md", "Ünïcode.md", "한글 이름.md"]
    )
    def test_일반_글자는_허용(self, name: str) -> None:
        assert check_changed_paths([f"wiki/{name}"]).ok
        assert check_changed_paths([unicodedata.normalize("NFD", f"wiki/{name}")]).ok

    def test_ZWJ_이모지는_위반이다(self) -> None:
        assert not check_changed_paths(["wiki/\U0001f468‍\U0001f469.md"]).ok


class TestWriteRootUnreadable:
    @pytest.fixture(autouse=True)
    def _not_root(self) -> None:
        if os.geteuid() == 0:
            pytest.skip("root는 권한 검사를 건너뛴다")

    @pytest.mark.parametrize("mode", [0o000, 0o300])
    def test_읽을_수_없는_하위_폴더_안의_링크는_통과하지_못한다(
        self, tmp_dir: Path, mode: int
    ) -> None:
        write_tree(tmp_dir, {"wiki/a.md": "a", "wiki/sub/b.md": "b"})
        os.symlink("/etc", tmp_dir / "wiki" / "sub" / "esc")
        os.chmod(tmp_dir / "wiki" / "sub", mode)
        try:
            reason = check_write_root(tmp_dir)
        finally:
            os.chmod(tmp_dir / "wiki" / "sub", 0o700)
        assert reason is not None
        assert reason.startswith("wiki 아래에 읽을 수 없는 폴더가 있습니다: ")
        assert "sub" in reason

    @pytest.mark.parametrize("mode", [0o000, 0o300])
    def test_wiki_자체를_읽을_수_없어도_통과하지_못한다(self, tmp_dir: Path, mode: int) -> None:
        write_tree(tmp_dir, {"wiki/a.md": "a"})
        os.chmod(tmp_dir / "wiki", mode)
        try:
            reason = check_write_root(tmp_dir)
        finally:
            os.chmod(tmp_dir / "wiki", 0o700)
        assert reason is not None
        assert "읽을 수 없는 폴더" in reason

    def test_읽을_수_없는_폴더_이름은_가려진다(self, tmp_dir: Path) -> None:
        token = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"
        write_tree(tmp_dir, {f"wiki/{token}/b.md": "b", "wiki/a\nb/c.md": "c"})
        for name in (token, "a\nb"):
            os.chmod(tmp_dir / "wiki" / name, 0o000)
        try:
            reason = check_write_root(tmp_dir)
        finally:
            for name in (token, "a\nb"):
                os.chmod(tmp_dir / "wiki" / name, 0o700)
        assert reason is not None
        assert token not in reason
        assert "\n" not in reason
        assert "<이름 생략>" in reason

    def test_링크와_읽을_수_없는_폴더가_함께_있으면_둘_다_알린다(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/sub/b.md": "b"})
        os.symlink(tmp_dir / "nowhere", tmp_dir / "wiki" / "l.md")
        os.chmod(tmp_dir / "wiki" / "sub", 0o000)
        try:
            reason = check_write_root(tmp_dir)
        finally:
            os.chmod(tmp_dir / "wiki" / "sub", 0o700)
        assert reason is not None
        assert "심볼릭 링크" in reason and "l.md" in reason
        assert "읽을 수 없는 폴더" in reason and "sub" in reason

    def test_읽을_수_없는_폴더는_최대_5개만_싣는다(self, tmp_dir: Path) -> None:
        for index in range(8):
            write_tree(tmp_dir, {f"wiki/d{index}/x.md": "x"})
            os.chmod(tmp_dir / "wiki" / f"d{index}", 0o000)
        try:
            reason = check_write_root(tmp_dir)
        finally:
            for index in range(8):
                os.chmod(tmp_dir / "wiki" / f"d{index}", 0o700)
        assert reason is not None
        assert sum(f"d{i}" in reason for i in range(8)) == 5

    def test_읽을_수_있는_폴더는_그대로_통과(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/sub/b.md": "b"})
        os.chmod(tmp_dir / "wiki" / "sub", 0o500)
        try:
            assert check_write_root(tmp_dir) is None
        finally:
            os.chmod(tmp_dir / "wiki" / "sub", 0o700)


class TestWalkBounds:
    def test_항목이_너무_많으면_통과시키지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(post_check, "_MAX_WALK_ENTRIES", 10)
        write_tree(tmp_dir, {f"wiki/{i}.md": "x" for i in range(30)})
        reason = check_write_root(tmp_dir)
        assert reason is not None
        assert "너무 많" in reason

    def test_깊은_폴더도_반복문으로_걷는다(self, tmp_dir: Path) -> None:
        deep = tmp_dir / "wiki"
        for _ in range(60):
            deep = deep / "d"
        deep.mkdir(parents=True)
        os.symlink(tmp_dir, deep / "esc")
        reason = check_write_root(tmp_dir)
        assert reason is not None and "esc" in reason
