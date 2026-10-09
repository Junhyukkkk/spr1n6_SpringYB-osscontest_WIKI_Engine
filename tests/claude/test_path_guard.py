import os
import posixpath
import random
import time
import unicodedata
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.claude import path_guard
from team_wiki_engine.claude.path_guard import (
    READ_TOOLS,
    WRITE_TOOLS,
    Allow,
    Decision,
    Deny,
    GuardContext,
    canonicalize,
    cli_glob_base,
    cli_glob_search_dir,
    decide,
    glob_static_prefix,
    is_inside,
    resolve_tool_path,
)
from tests.helpers import write_tree


@pytest.fixture
def root(tmp_dir: Path) -> Path:
    return tmp_dir


@pytest.fixture
def data(root: Path) -> Path:
    return root / "data"


@pytest.fixture
def repos(root: Path) -> Path:
    return root / "repos"


@pytest.fixture
def outside(root: Path) -> Path:
    return root / "outside"


@pytest.fixture
def ctx(root: Path, data: Path, repos: Path) -> GuardContext:
    write_tree(
        root,
        {
            "data/wiki/index.md": "# 목차",
            "data/raw/a.md": "원본",
            "repos/worker/src/A.kt": "class A",
            "repos/worker/.env": "SECRET=1",
            "repos/worker/.env.example": "SECRET=",
            "outside/secret.txt": "top-secret",
        },
    )
    return GuardContext(cwd=data, read_roots=(repos,), write_roots=(data / "wiki",))


def deny_reason(d: Decision) -> str:
    assert isinstance(d, Deny), f"허용되어 버림: {d}"
    return d.reason


def assert_allowed(d: Decision) -> None:
    assert d == Allow(), d


def assert_denied(d: Decision, label: str = "") -> None:
    assert isinstance(d, Deny), f"거부되어야 하는데 허용됨: {label}"
    assert d.allow is False
    assert d.reason != ""


# ---------------------------------------------------------------- 읽기


class TestRead:
    def test_작업_폴더와_읽기_루트_안은_허용한다(
        self, ctx: GuardContext, data: Path, repos: Path
    ) -> None:
        assert_allowed(decide("Read", {"file_path": str(data / "wiki/index.md")}, ctx))
        assert_allowed(decide("Read", {"file_path": "wiki/index.md"}, ctx))
        assert_allowed(decide("Read", {"file_path": str(repos / "worker/src/A.kt")}, ctx))

    def test_루트_밖과_점점_경로는_거부한다(self, ctx: GuardContext, outside: Path) -> None:
        assert_denied(decide("Read", {"file_path": str(outside / "secret.txt")}, ctx))
        assert_denied(decide("Read", {"file_path": "../outside/secret.txt"}, ctx))

    def test_env는_루트_안이어도_거부하고_env_example은_허용한다(
        self, ctx: GuardContext, repos: Path
    ) -> None:
        assert_denied(decide("Read", {"file_path": str(repos / "worker/.env")}, ctx))
        assert_denied(
            decide("Grep", {"path": str(repos / "worker/.env"), "pattern": "SECRET"}, ctx)
        )
        assert_allowed(decide("Read", {"file_path": str(repos / "worker/.env.example")}, ctx))

    def test_grep은_path가_없으면_작업_폴더로_본다(self, ctx: GuardContext, outside: Path) -> None:
        assert_allowed(decide("Grep", {"pattern": "x"}, ctx))
        assert_denied(decide("Grep", {"pattern": "x", "path": str(outside)}, ctx))

    def test_경로를_알_수_없는_입력은_거부한다(self, ctx: GuardContext) -> None:
        assert_denied(decide("Read", {}, ctx))
        assert_denied(decide("Read", {"file_path": "   "}, ctx))
        assert_denied(decide("Read", {"file_path": ""}, ctx))


# ---------------------------------------------------------------- Glob 접두어


class TestGlobStaticPrefix:
    def test_고정_접두어를_뽑는다(self) -> None:
        assert glob_static_prefix("/etc/*.conf") == "/etc"
        assert glob_static_prefix("/**") == "/"
        assert glob_static_prefix("wiki/**/*.md") == "wiki"
        assert glob_static_prefix("**/*.md") == "."
        assert glob_static_prefix("../../*") == "../.."

    def test_글롭_문자가_든_구간_앞까지만_취한다(self) -> None:
        assert glob_static_prefix("/etc/pass?d") == "/etc"
        assert glob_static_prefix("/a/b[c]/d") == "/a"
        assert glob_static_prefix("/a/{b,c}/d") == "/a"
        assert glob_static_prefix("wiki/a.md") == "wiki/a.md"

    def test_역슬래시로_시작하는_패턴도_절대로_본다(self) -> None:
        assert glob_static_prefix("\\**") == "/"


class TestGlob:
    def test_절대_패턴과_상위_패턴은_거부한다(self, ctx: GuardContext, outside: Path) -> None:
        assert_denied(decide("Glob", {"pattern": f"{outside}/*"}, ctx))
        assert_denied(decide("Glob", {"pattern": "../../**"}, ctx))
        assert_denied(decide("Glob", {"pattern": "/etc/*"}, ctx))

    def test_루트_안_패턴은_허용한다(self, ctx: GuardContext, repos: Path) -> None:
        assert_allowed(decide("Glob", {"pattern": "wiki/**/*.md"}, ctx))
        assert_allowed(decide("Glob", {"pattern": "**/*.kt", "path": str(repos)}, ctx))

    def test_절대_패턴이_루트_안이면_허용한다(self, ctx: GuardContext, data: Path) -> None:
        assert_allowed(decide("Glob", {"pattern": f"{data}/wiki/**/*.md"}, ctx))

    def test_글롭_문자_뒤나_중괄호_안의_점점도_거부한다(self, ctx: GuardContext) -> None:
        for pattern in (
            "**/../../outside/*",
            "{wiki,../outside}/*",
            "wiki/**/../../x",
            "wiki\\**\\..\\..\\x",
        ):
            assert_denied(decide("Glob", {"pattern": pattern}, ctx), pattern)

    def test_거부_사유에_점점이_들어_있다(self, ctx: GuardContext) -> None:
        assert ".." in deny_reason(decide("Glob", {"pattern": "**/../x"}, ctx))

    def test_이름_안의_점_두_개는_허용한다(self, ctx: GuardContext) -> None:
        assert_allowed(decide("Glob", {"pattern": "a..b/*.md"}, ctx))

    def test_중괄호_확장으로_만든_점점은_거부한다(self, ctx: GuardContext) -> None:
        for pattern in (
            ".{.,x}/*",
            "{.,x}{.,y}/*",
            "{wiki,..}/*",
            "{a,{.,x}.}/*",
            "{x,.}{.}/*",
        ):
            assert_denied(decide("Glob", {"pattern": pattern}, ctx), pattern)

    def test_짝_없는_괄호_옆의_점점도_거부한다(self, ctx: GuardContext) -> None:
        # 짝이 안 맞는 중괄호를 다르게 해석하는 엔진에 대비해 원문도 `{},`로 쪼개어 본다.
        for pattern in ("{..,x/*", "wiki/x,..}/*", "wiki/{..", "..}/x", "a,../*"):
            assert_denied(decide("Glob", {"pattern": pattern}, ctx), pattern)

    def test_확장_결과가_너무_많으면_거부하고_사유를_준다(
        self, ctx: GuardContext, data: Path
    ) -> None:
        many = "{a,b}" * 9
        d = decide("Glob", {"pattern": f"wiki/{many}"}, ctx)
        assert "중괄호 확장이 너무 많습니다" in deny_reason(d)
        assert resolve_tool_path("Glob", {"pattern": f"wiki/{many}"}, data) is None

    def test_확장_개수_256개는_허용한다(self, ctx: GuardContext) -> None:
        assert_allowed(decide("Glob", {"pattern": "wiki/" + "{a,b}" * 8}, ctx))

    def test_정상_패턴은_허용한다(self, ctx: GuardContext) -> None:
        for pattern in (
            "**/*.{kt,java}",
            "{wiki,raw}/**/*.md",
            "a..b/*.md",
            "wiki/**/*.md",
        ):
            assert_allowed(decide("Glob", {"pattern": pattern}, ctx))

    def test_짝이_맞지_않는_중괄호는_글자_그대로_본다(self, ctx: GuardContext) -> None:
        assert_allowed(decide("Glob", {"pattern": "wiki/{a/*.md"}, ctx))
        assert_allowed(decide("Glob", {"pattern": "wiki/a}/*.md"}, ctx))

    def test_resolve_tool_path도_확장된_점점을_none으로_돌려준다(self, data: Path) -> None:
        assert resolve_tool_path("Glob", {"pattern": ".{.,x}/*"}, data) is None

    def test_패턴이_2048자를_넘으면_거부한다(self, ctx: GuardContext) -> None:
        assert_denied(decide("Glob", {"pattern": "a" * 3000}, ctx))
        assert_allowed(decide("Glob", {"pattern": "*" + "a" * 2047}, ctx))
        assert_denied(decide("Glob", {"pattern": "*" + "a" * 2048}, ctx))

    def test_중괄호_6만개_패턴도_1초_안에_판정한다(self, ctx: GuardContext) -> None:
        for pattern in ("{" * 60_000, "{a," * 20_000, "{" * 1500 + "}" * 1500):
            started = time.perf_counter()
            assert_denied(decide("Glob", {"pattern": pattern}, ctx))
            assert time.perf_counter() - started < 1.0

    def test_2048자_안의_악성_패턴도_빠르게_판정한다(self, ctx: GuardContext) -> None:
        patterns = [
            "{" * 1024 + "}" * 1024,
            "{a}" * 680,
            "{a,b}" * 8 + "{x}" * 600,
            "{a,{b,{c,{d,{e,{f,{g,{h,{i,j}}}}}}}}}" * 40,
            "{" + ",".join(["a"] * 600) + "}",
            "{}" * 1000,
        ]
        for pattern in patterns:
            started = time.perf_counter()
            decide("Glob", {"pattern": pattern}, ctx)
            assert time.perf_counter() - started < 1.0, pattern[:30]

    def test_중괄호_중첩이_너무_깊으면_거부한다(self, ctx: GuardContext) -> None:
        assert_denied(decide("Glob", {"pattern": "{a," * 40 + "b" + "}" * 40}, ctx))

    def test_정당한_패턴은_중첩_몇_겹까지_허용한다(self, ctx: GuardContext) -> None:
        assert_allowed(decide("Glob", {"pattern": "{wiki,{raw,x}}/**/*.md"}, ctx))

    def test_패턴이_물결표나_앞뒤_공백이면_거부한다(self, ctx: GuardContext) -> None:
        for pattern in ("~/.ssh/*", "~", " /etc/*", "*.md\n", "\t*.md"):
            assert_denied(decide("Glob", {"pattern": pattern}, ctx), repr(pattern))

    def test_패턴_중간의_물결표는_허용한다(self, ctx: GuardContext) -> None:
        assert_allowed(decide("Glob", {"pattern": "wiki/*~"}, ctx))

    def test_패턴이_문자열이_아니면_거부한다(self, ctx: GuardContext) -> None:
        for bad in (5, ["*"], b"*", {"a": 1}, None, True):
            assert_denied(decide("Glob", {"pattern": bad}, ctx), repr(bad))

    def test_글롭이_심볼릭_링크로_밖을_가리키면_거부한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        os.symlink(outside, data / "wiki" / "dirlink")
        assert_denied(decide("Glob", {"pattern": "wiki/dirlink/*"}, ctx))
        assert_denied(decide("Glob", {"pattern": "*", "path": str(data / "wiki/dirlink")}, ctx))

    def test_글롭_path_밖이면_패턴이_안쪽이어도_거부한다(
        self, ctx: GuardContext, outside: Path
    ) -> None:
        assert_denied(decide("Glob", {"pattern": "*.txt", "path": str(outside)}, ctx))


# ---------------------------------------------------------------- 쓰기


class TestWrite:
    def test_쓰기_루트_안만_허용한다(
        self, ctx: GuardContext, data: Path, repos: Path, outside: Path
    ) -> None:
        assert_allowed(decide("Edit", {"file_path": str(data / "wiki/index.md")}, ctx))
        assert_denied(decide("Write", {"file_path": str(data / "raw/b.md")}, ctx))
        assert_denied(decide("Write", {"file_path": str(repos / "worker/src/B.kt")}, ctx))
        assert_denied(decide("Edit", {"file_path": str(outside / "secret.txt")}, ctx))

    def test_아직_없는_하위_폴더에도_쓰기_루트_안이면_허용한다(
        self, ctx: GuardContext, data: Path
    ) -> None:
        target = data / "wiki/decisions/2026-10-07-새결정.md"
        assert_allowed(decide("Write", {"file_path": str(target)}, ctx))
        assert not target.parent.exists()

    def test_쓰기_루트가_없으면_모든_쓰기를_거부한다(self, ctx: GuardContext, data: Path) -> None:
        read_only = GuardContext(cwd=ctx.cwd, read_roots=ctx.read_roots, write_roots=())
        assert_denied(decide("Write", {"file_path": str(data / "wiki/x.md")}, read_only))

    def test_상대_경로_쓰기도_허용한다(self, ctx: GuardContext) -> None:
        assert_allowed(decide("Write", {"file_path": "wiki/new.md"}, ctx))
        assert_denied(decide("Write", {"file_path": "new.md"}, ctx))

    def test_git_세그먼트가_든_경로에는_쓰지_못한다(self, ctx: GuardContext, data: Path) -> None:
        assert_denied(decide("Write", {"file_path": str(data / "wiki/.git/config")}, ctx))
        assert_denied(decide("Edit", {"file_path": "wiki/.GIT/hooks/pre-commit"}, ctx))
        assert_denied(decide("Write", {"file_path": "wiki/.git"}, ctx))
        assert_allowed(decide("Write", {"file_path": "wiki/.gitkeep"}, ctx))
        assert_allowed(decide("Write", {"file_path": "wiki/a.git/x.md"}, ctx))

    def test_쓰기_경로의_점점은_거부한다(self, ctx: GuardContext, data: Path) -> None:
        assert_denied(decide("Write", {"file_path": "wiki/../wiki/x.md"}, ctx))
        assert_denied(decide("Write", {"file_path": str(data / "wiki/../raw/x.md")}, ctx))

    def test_env_파일_쓰기는_거부한다(self, ctx: GuardContext) -> None:
        assert_denied(decide("Write", {"file_path": "wiki/.env"}, ctx))
        assert_allowed(decide("Write", {"file_path": "wiki/.env.example"}, ctx))


# ---------------------------------------------------------------- 도구


class TestTools:
    def test_목록에_없는_도구는_거부한다(self, ctx: GuardContext) -> None:
        assert_denied(decide("Bash", {"command": "ls"}, ctx))
        assert_denied(decide("WebFetch", {"url": "https://example.com"}, ctx))
        assert_denied(decide("NotebookEdit", {"notebook_path": "wiki/a.ipynb"}, ctx))
        assert_denied(decide("read", {"file_path": "wiki/index.md"}, ctx))

    def test_거부_사유를_문자열로_준다(self, ctx: GuardContext) -> None:
        assert deny_reason(decide("Bash", {"command": "ls"}, ctx)) != ""

    def test_도구_집합(self) -> None:
        assert READ_TOOLS == frozenset({"Read", "Grep", "Glob"})
        assert WRITE_TOOLS == frozenset({"Edit", "Write"})


class TestDecisionShape:
    def test_allow_불리언과_거부_사유로_좁혀진다(self, ctx: GuardContext) -> None:
        d = decide("Bash", {}, ctx)
        if d.allow:
            raise AssertionError("거부되어야 함")
        assert "Bash" in d.reason
        ok = decide("Read", {"file_path": "wiki/index.md"}, ctx)
        assert ok.allow is True

    def test_결정은_불변이다(self) -> None:
        with pytest.raises(AttributeError):
            Deny("x").reason = "y"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            Allow().allow = True  # type: ignore[misc]

    def test_deny는_사유가_있어야_한다(self) -> None:
        with pytest.raises(TypeError):
            Deny()  # type: ignore[call-arg]


# ---------------------------------------------------------------- 심볼릭 링크


class TestSymlinks:
    def test_루트_안에서_밖을_가리키는_링크는_거부한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        os.symlink(outside / "secret.txt", data / "wiki" / "leak.md")
        assert_denied(decide("Read", {"file_path": str(data / "wiki" / "leak.md")}, ctx))

    def test_링크로_넘긴_루트도_같은_폴더로_인정한다(
        self, ctx: GuardContext, root: Path, data: Path, repos: Path
    ) -> None:
        link = root / "data-link"
        os.symlink(data, link)
        linked = GuardContext(cwd=link, read_roots=(repos,), write_roots=(link / "wiki",))
        assert_allowed(decide("Read", {"file_path": str(data / "wiki/index.md")}, linked))
        assert_allowed(decide("Write", {"file_path": str(data / "wiki/new.md")}, linked))
        assert_allowed(decide("Write", {"file_path": str(link / "wiki/new.md")}, linked))
        assert_allowed(decide("Write", {"file_path": str(link / "wiki/sub/new.md")}, linked))
        assert_allowed(decide("Read", {"file_path": "wiki/index.md"}, linked))

    def test_쓰기_루트가_링크로_넘어와도_밑으로_더_내려가는_링크는_거부한다(
        self, ctx: GuardContext, root: Path, data: Path, outside: Path
    ) -> None:
        link = root / "data-link"
        os.symlink(data, link)
        os.symlink(outside, data / "wiki" / "dirlink")
        linked = GuardContext(cwd=link, read_roots=(), write_roots=(link / "wiki",))
        assert_denied(decide("Write", {"file_path": str(link / "wiki/dirlink/a.md")}, linked))

    def test_realpath하지_않은_임시_폴더에서도_동작한다(self, raw_tmp_dir: Path) -> None:
        (raw_tmp_dir / "wiki").mkdir()
        (raw_tmp_dir / "wiki" / "a.md").write_text("a", encoding="utf-8")
        raw_ctx = GuardContext(cwd=raw_tmp_dir, read_roots=(), write_roots=(raw_tmp_dir / "wiki",))
        assert_allowed(decide("Read", {"file_path": str(raw_tmp_dir / "wiki" / "a.md")}, raw_ctx))
        assert_allowed(decide("Write", {"file_path": str(raw_tmp_dir / "wiki" / "b.md")}, raw_ctx))
        assert_allowed(decide("Write", {"file_path": "wiki/sub/c.md"}, raw_ctx))
        assert_allowed(decide("Edit", {"file_path": str(raw_tmp_dir / "wiki" / "a.md")}, raw_ctx))
        assert_allowed(decide("Grep", {"pattern": "a"}, raw_ctx))
        assert_allowed(decide("Glob", {"pattern": "wiki/*.md"}, raw_ctx))

    def test_깨진_링크로_쓰기_루트_밖에_쓰지_못한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        os.symlink(outside / "없는파일", data / "wiki" / "evil.md")
        assert not (data / "wiki" / "evil.md").exists()
        assert_denied(decide("Write", {"file_path": str(data / "wiki" / "evil.md")}, ctx))
        assert_denied(decide("Edit", {"file_path": "wiki/evil.md"}, ctx))

    def test_링크가_가리키는_대상이_존재해도_쓰기는_거부한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        os.symlink(outside / "secret.txt", data / "wiki" / "evil.md")
        assert_denied(decide("Write", {"file_path": str(data / "wiki" / "evil.md")}, ctx))

    def test_쓰기_루트_안을_가리키는_링크도_쓰기는_거부한다(
        self, ctx: GuardContext, data: Path
    ) -> None:
        os.symlink(data / "wiki" / "index.md", data / "wiki" / "alias.md")
        assert_denied(decide("Write", {"file_path": str(data / "wiki" / "alias.md")}, ctx))
        # 읽기는 링크를 따라간 결과가 루트 안이면 허용한다.
        assert_allowed(decide("Read", {"file_path": str(data / "wiki" / "alias.md")}, ctx))

    def test_깨진_디렉터리_링크_아래_쓰기도_거부한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        os.symlink(outside / "없는폴더", data / "wiki" / "dirlink")
        assert_denied(decide("Write", {"file_path": str(data / "wiki/dirlink/a.md")}, ctx))
        assert_denied(decide("Write", {"file_path": "wiki/dirlink/sub/deep/a.md"}, ctx))

    def test_존재하는_디렉터리_링크_아래_쓰기도_거부한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        os.symlink(outside, data / "wiki" / "dirlink")
        assert_denied(decide("Write", {"file_path": str(data / "wiki/dirlink/a.md")}, ctx))
        assert_denied(decide("Edit", {"file_path": str(data / "wiki/dirlink/secret.txt")}, ctx))

    def test_루트_안_다른_폴더를_가리키는_디렉터리_링크_아래_쓰기도_거부한다(
        self, ctx: GuardContext, data: Path
    ) -> None:
        os.symlink(data / "wiki", data / "wiki" / "selfloop")
        assert_denied(decide("Write", {"file_path": "wiki/selfloop/a.md"}, ctx))

    def test_링크_없는_평범한_새_파일은_허용한다(self, ctx: GuardContext, data: Path) -> None:
        assert_allowed(decide("Write", {"file_path": str(data / "wiki" / "plain.md")}, ctx))
        assert_allowed(decide("Write", {"file_path": str(data / "wiki/a/b/c/plain.md")}, ctx))

    def test_링크_체인으로_밖에_닿아도_거부한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        os.symlink(outside / "secret.txt", data / "wiki" / "hop2")
        os.symlink(data / "wiki" / "hop2", data / "wiki" / "hop1")
        assert_denied(decide("Read", {"file_path": str(data / "wiki" / "hop1")}, ctx))
        assert_denied(decide("Write", {"file_path": str(data / "wiki" / "hop1")}, ctx))

    def test_env를_가리키는_링크는_읽기도_거부한다(
        self, ctx: GuardContext, data: Path, repos: Path
    ) -> None:
        os.symlink(repos / "worker" / ".env", data / "wiki" / "innocent.md")
        assert_denied(decide("Read", {"file_path": str(data / "wiki" / "innocent.md")}, ctx))

    def test_env라는_이름의_링크는_가리키는_곳이_다른_이름이어도_거부한다(
        self, ctx: GuardContext, repos: Path
    ) -> None:
        os.symlink(repos / "worker" / "src" / "A.kt", repos / "worker" / "src" / ".env")
        assert_denied(decide("Read", {"file_path": str(repos / "worker/src/.env")}, ctx))

    def test_링크_루프는_예외_없이_거부한다(self, ctx: GuardContext, data: Path) -> None:
        os.symlink(data / "wiki" / "loop-b", data / "wiki" / "loop-a")
        os.symlink(data / "wiki" / "loop-a", data / "wiki" / "loop-b")
        assert_denied(decide("Write", {"file_path": str(data / "wiki" / "loop-a")}, ctx))
        assert_denied(decide("Read", {"file_path": str(data / "wiki" / "loop-a")}, ctx))
        assert_denied(decide("Write", {"file_path": str(data / "wiki" / "loop-a" / "x.md")}, ctx))

    def test_링크_뒤_점점은_물리_해석으로도_검사한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        (outside / "sub").mkdir()
        (outside / "secret2.txt").write_text("s", encoding="utf-8")
        os.symlink(outside / "sub", data / "wiki" / "dirlink")
        # 문자열로만 접으면 wiki/secret2.txt(안), 실제 OS는 outside/secret2.txt(밖).
        assert_denied(decide("Read", {"file_path": "wiki/dirlink/../secret2.txt"}, ctx))

    def test_루트_자체가_링크여도_안쪽_접근은_허용한다(
        self, ctx: GuardContext, root: Path, repos: Path
    ) -> None:
        link = root / "repos-link"
        os.symlink(repos, link)
        linked = GuardContext(cwd=ctx.cwd, read_roots=(link,), write_roots=())
        assert_allowed(decide("Read", {"file_path": str(repos / "worker/src/A.kt")}, linked))
        assert_allowed(decide("Read", {"file_path": str(link / "worker/src/A.kt")}, linked))


# ---------------------------------------------------------------- 경계 사례


class TestBoundaries:
    def test_형제_접두어_루트를_구분한다(self, root: Path) -> None:
        write_tree(root, {"data/a.md": "a", "data-evil/a.md": "x"})
        c = GuardContext(cwd=root / "data", read_roots=(), write_roots=(root / "data",))
        assert_allowed(decide("Read", {"file_path": str(root / "data/a.md")}, c))
        assert_denied(decide("Read", {"file_path": str(root / "data-evil/a.md")}, c))
        assert_denied(decide("Write", {"file_path": str(root / "data-evil/b.md")}, c))
        assert_denied(decide("Glob", {"pattern": str(root / "data-evil") + "/*"}, c))
        assert not is_inside(root / "data-evil", [root / "data"])
        assert is_inside(root / "data", [root / "data"])
        assert is_inside(root / "data" / "x" / "y", [root / "data"])

    def test_루트가_슬래시면_모든_경로가_안이다(self, tmp_dir: Path) -> None:
        assert is_inside(tmp_dir, [Path("/")])
        assert is_inside(Path("/"), [Path("/")])
        c = GuardContext(cwd=tmp_dir, read_roots=(Path("/"),), write_roots=())
        assert_allowed(decide("Read", {"file_path": "/etc/hosts"}, c))
        assert_denied(decide("Read", {"file_path": "/etc/.ENV"}, c))
        assert_denied(decide("Write", {"file_path": "/etc/x"}, c))

    def test_is_inside_빈_루트_목록은_거짓이다(self, tmp_dir: Path) -> None:
        assert not is_inside(tmp_dir, [])

    def test_연속_슬래시와_점과_끝_슬래시를_정리한다(self, ctx: GuardContext, data: Path) -> None:
        assert_allowed(decide("Read", {"file_path": f"{data}//wiki///index.md"}, ctx))
        assert_allowed(decide("Read", {"file_path": f"{data}/./wiki/./index.md"}, ctx))
        assert_allowed(decide("Read", {"file_path": f"{data}/wiki/"}, ctx))
        assert_allowed(decide("Write", {"file_path": f"{data}//wiki//x.md"}, ctx))
        assert_denied(decide("Read", {"file_path": f"{data}/wiki/../../outside/secret.txt"}, ctx))
        assert_denied(decide("Read", {"file_path": f"{data}/../outside/./secret.txt/"}, ctx))
        assert_denied(decide("Read", {"file_path": "//etc/passwd"}, ctx))
        assert_denied(decide("Read", {"file_path": "/etc//passwd"}, ctx))

    def test_env에_슬래시나_점을_붙여도_거부한다(self, ctx: GuardContext, repos: Path) -> None:
        env = repos / "worker/.env"
        assert_denied(decide("Read", {"file_path": f"{env}/"}, ctx))
        assert_denied(decide("Read", {"file_path": f"{env}/."}, ctx))
        assert_denied(decide("Read", {"file_path": f"{repos}/worker/./.env"}, ctx))
        assert_denied(decide("Read", {"file_path": f"{repos}/worker/src/../.env"}, ctx))
        assert_denied(decide("Read", {"file_path": str(repos / "worker/.ENV")}, ctx))
        assert_denied(decide("Read", {"file_path": str(repos / "worker/.Env")}, ctx))
        assert_allowed(decide("Read", {"file_path": str(repos / "worker/.env.example")}, ctx))

    def test_env는_글롭_접두어로도_거부한다(self, ctx: GuardContext, repos: Path) -> None:
        assert_denied(decide("Glob", {"pattern": f"{repos}/worker/.env"}, ctx))
        assert_denied(decide("Glob", {"pattern": "*", "path": str(repos / "worker/.ENV")}, ctx))

    def test_nul_문자가_든_입력은_거부한다(self, ctx: GuardContext, data: Path) -> None:
        for tool, field in (("Read", "file_path"), ("Write", "file_path"), ("Grep", "path")):
            assert_denied(decide(tool, {field: f"{data}/wiki/a\x00.md"}, ctx), tool)
            assert_denied(decide(tool, {field: "\x00"}, ctx), tool)
        assert_denied(decide("Glob", {"pattern": "wiki/\x00*"}, ctx))
        assert_denied(decide("Glob", {"pattern": "*", "path": "wiki\x00"}, ctx))

    def test_js_trim이_자르는_문자도_거부한다(self, ctx: GuardContext) -> None:
        # Python의 str.strip()은 BOM(\ufeff)을 자르지 않지만 JS trim()은 자른다.
        for p in (
            "\ufeff/etc/passwd",
            "/etc/passwd\ufeff",
            chr(0xA0) + "/etc",
            chr(0x3000) + "/etc",
            "/etc" + chr(0x2028),
        ):
            assert_denied(decide("Read", {"file_path": p}, ctx), repr(p))
            assert_denied(decide("Grep", {"path": p, "pattern": "x"}, ctx), repr(p))

    def test_물결표와_공백_경로를_모든_도구에서_거부한다(
        self, ctx: GuardContext, repos: Path, outside: Path
    ) -> None:
        env = str(repos / "worker/.env")
        for p in (
            "~/.aws",
            "~",
            " /etc",
            "\n/etc",
            "~/.ssh/id_rsa",
            " /etc/passwd",
            "\t/etc/passwd",
            "/etc ",
            f"{outside} ",
            "wiki/index.md ",
            " wiki/index.md",
            f"{env} ",
            f"{env}\n",
            ".env ",
            "~root/x",
        ):
            for tool in ("Read", "Edit", "Write"):
                assert_denied(decide(tool, {"file_path": p}, ctx), f"{tool} {p!r}")
            assert_denied(decide("Grep", {"pattern": "x", "path": p}, ctx), f"Grep {p!r}")
            assert_denied(decide("Glob", {"pattern": "*", "path": p}, ctx), f"Glob {p!r}")

    def test_경로_중간의_물결표와_공백은_허용한다(self, ctx: GuardContext, data: Path) -> None:
        assert_allowed(decide("Read", {"file_path": "wiki/a~b.md"}, ctx))
        assert_allowed(decide("Read", {"file_path": "wiki/~foo.md"}, ctx))
        assert_allowed(decide("Read", {"file_path": "wiki/my file.md"}, ctx))
        assert_allowed(decide("Write", {"file_path": str(data / "wiki/my file~1.md")}, ctx))
        assert_allowed(decide("Write", {"file_path": "wiki/~foo.md"}, ctx))

    def test_경로_없는_grep과_glob은_cwd로_허용한다(self, ctx: GuardContext) -> None:
        assert_allowed(decide("Grep", {"pattern": "x"}, ctx))
        assert_allowed(decide("Glob", {"pattern": "*.md"}, ctx))
        assert_allowed(decide("Grep", {"pattern": "x", "path": ""}, ctx))
        assert_allowed(decide("Glob", {"pattern": "*.md", "path": ""}, ctx))

    def test_경로_자리에_문자열이_아닌_값이_있으면_거부한다(self, ctx: GuardContext) -> None:
        for bad in (None, 0, 1.5, True, ["/etc"], {"a": 1}, b"/etc", ("a",)):
            assert_denied(decide("Read", {"file_path": bad}, ctx), repr(bad))
            assert_denied(decide("Write", {"file_path": bad}, ctx), repr(bad))
            assert_denied(decide("Grep", {"pattern": "x", "path": bad}, ctx), repr(bad))
            assert_denied(decide("Glob", {"pattern": "*", "path": bad}, ctx), repr(bad))

    def test_grep의_glob_필드는_검색_루트를_바꾸지_못한다(
        self, ctx: GuardContext, outside: Path
    ) -> None:
        # rg --glob 은 path 아래를 거르는 필터일 뿐이다. 판정은 path 만 본다.
        assert_allowed(decide("Grep", {"pattern": "x", "glob": "*.md"}, ctx))
        assert_allowed(decide("Grep", {"pattern": "x", "glob": f"{outside}/*"}, ctx))
        assert_denied(decide("Grep", {"pattern": "x", "path": str(outside), "glob": "*.md"}, ctx))

    def test_resolve_tool_path(self, data: Path, ctx: GuardContext) -> None:
        assert resolve_tool_path("Bash", {"command": "ls"}, data) is None
        assert resolve_tool_path("Read", {"file_path": "wiki/index.md"}, data) == (
            data / "wiki/index.md"
        )
        assert resolve_tool_path("Read", {"file_path": "~/x"}, data) is None
        assert resolve_tool_path("Grep", {"path": " /etc"}, data) is None
        assert resolve_tool_path("Glob", {"pattern": "*", "path": "~"}, data) is None
        assert resolve_tool_path("Write", {"file_path": "wiki/a.md "}, data) is None
        assert resolve_tool_path("Read", {}, data) is None
        assert resolve_tool_path("Grep", {}, data) == data
        assert resolve_tool_path("Glob", {"pattern": "wiki/**/*.md"}, data) == data / "wiki"
        assert resolve_tool_path("Glob", {"pattern": "**/*.md"}, data) == data
        assert resolve_tool_path("Read", {"file_path": 3}, data) is None
        assert ctx.cwd == data


class TestCanonicalize:
    def test_없는_경로는_가장_가까운_조상을_해소하고_나머지를_붙인다(
        self, root: Path, data: Path
    ) -> None:
        os.symlink(root, root.parent / f"{root.name}-alias")
        try:
            alias = root.parent / f"{root.name}-alias"
            assert canonicalize(alias / "no" / "such" / "file.md") == root / "no/such/file.md"
            assert canonicalize(str(alias / "x")) == root / "x"
        finally:
            (root.parent / f"{root.name}-alias").unlink()
        assert data == root / "data"

    def test_macos_var_경로를_private_var로_바꾼다(self, raw_tmp_dir: Path) -> None:
        real = Path(os.path.realpath(raw_tmp_dir))
        assert canonicalize(raw_tmp_dir / "없음.md") == real / "없음.md"

    def test_깨진_링크는_가리키는_곳으로_해소한다(self, root: Path, outside: Path) -> None:
        os.symlink(outside / "nope", root / "dangling")
        assert canonicalize(root / "dangling") == outside / "nope"

    def test_점점은_링크를_해소한_뒤_접는다(self, root: Path, outside: Path) -> None:
        (outside / "sub").mkdir(parents=True)
        os.symlink(outside / "sub", root / "lnk")
        assert canonicalize(root / "lnk" / ".." / "x") == outside / "x"


# ---------------------------------------------------------------- GuardContext


class TestGuardContext:
    def test_상대_cwd를_거부한다(self, tmp_dir: Path) -> None:
        with pytest.raises(ValueError, match="cwd"):
            GuardContext(cwd=Path("relative/dir"), read_roots=(), write_roots=())

    def test_빈_cwd를_거부한다(self) -> None:
        with pytest.raises(ValueError, match="cwd"):
            GuardContext(cwd=Path(""), read_roots=(), write_roots=())

    def test_빈_문자열_루트를_거부한다(self, tmp_dir: Path) -> None:
        with pytest.raises(ValueError, match="read_roots"):
            GuardContext(cwd=tmp_dir, read_roots=(Path(""),), write_roots=())
        with pytest.raises(ValueError, match="write_roots"):
            GuardContext(cwd=tmp_dir, read_roots=(), write_roots=(Path(""),))

    def test_상대_루트를_거부한다(self, tmp_dir: Path) -> None:
        with pytest.raises(ValueError, match="read_roots"):
            GuardContext(cwd=tmp_dir, read_roots=(Path("repos"),), write_roots=())
        with pytest.raises(ValueError, match="write_roots"):
            GuardContext(cwd=tmp_dir, read_roots=(), write_roots=(Path("./wiki"),))
        with pytest.raises(ValueError, match="write_roots"):
            GuardContext(cwd=tmp_dir, read_roots=(), write_roots=(tmp_dir, Path("wiki")))

    def test_문자열_루트와_nul이_든_루트를_거부한다(self, tmp_dir: Path) -> None:
        bad_str: Any = ""
        with pytest.raises(ValueError, match="read_roots"):
            GuardContext(cwd=tmp_dir, read_roots=(bad_str,), write_roots=())
        with pytest.raises(ValueError, match="cwd"):
            GuardContext(cwd=Path("/tmp/a\x00b"), read_roots=(), write_roots=())

    def test_리스트로_받아도_튜플로_굳힌다(self, tmp_dir: Path) -> None:
        roots: Any = [tmp_dir]
        c = GuardContext(cwd=tmp_dir, read_roots=roots, write_roots=roots)
        assert c.read_roots == (tmp_dir,)
        assert c.write_roots == (tmp_dir,)

    def test_절대_경로는_받는다(self, tmp_dir: Path) -> None:
        c = GuardContext(cwd=tmp_dir, read_roots=(tmp_dir / "a",), write_roots=(tmp_dir / "b",))
        assert c.cwd == tmp_dir

    def test_불변이다(self, tmp_dir: Path) -> None:
        c = GuardContext(cwd=tmp_dir, read_roots=(), write_roots=())
        with pytest.raises(AttributeError):
            c.cwd = Path("/")  # type: ignore[misc]


# ---------------------------------------------------------------- 어떤 입력에도 예외 없음


class TestTotality:
    def test_이상한_입력에도_예외_없이_거부한다(self, ctx: GuardContext) -> None:
        weird: list[Any] = [
            None,
            0,
            3.14,
            [],
            ["wiki/index.md"],
            {"a": 1},
            "wiki/\x00index.md",
            "\x00",
            "a" * 100_000,
            "/" * 5000,
            "wiki/" + "a/" * 2000 + "x.md",
            "wiki/\ud800.md",
        ]
        for value in weird:
            for tool in ("Read", "Edit", "Write"):
                assert_denied(decide(tool, {"file_path": value}, ctx), f"{tool} {value!r:.30}")
            for tool in ("Grep", "Glob"):
                if value is not None and not isinstance(value, str):
                    assert_denied(decide(tool, {"path": value, "pattern": "*"}, ctx))
            if not isinstance(value, str) or len(value) > 2048 or "\x00" in value:
                assert_denied(decide("Glob", {"pattern": value}, ctx), f"Glob {value!r:.30}")

    def test_도구_이름과_입력_자체가_이상해도_거부한다(self, ctx: GuardContext) -> None:
        bad_name: Any = ["Read"]
        assert_denied(decide(bad_name, {"file_path": "wiki/index.md"}, ctx))
        bad_name = None
        assert_denied(decide(bad_name, {}, ctx))
        bad_input: Any = None
        assert_denied(decide("Read", bad_input, ctx))
        bad_input = ["file_path", "wiki/index.md"]
        assert_denied(decide("Read", bad_input, ctx))
        bad_input = "wiki/index.md"
        assert_denied(decide("Write", bad_input, ctx))

    def test_get이_터지는_매핑에도_예외_없이_거부한다(self, ctx: GuardContext) -> None:
        class Exploding(Mapping[str, object]):
            def __getitem__(self, key: str) -> object:
                raise RuntimeError("boom")

            def __iter__(self) -> Iterator[str]:
                raise RuntimeError("boom")

            def __len__(self) -> int:
                raise RuntimeError("boom")

        assert_denied(decide("Read", Exploding(), ctx))

    def test_긴_문자열은_빠르게_거부한다(self, ctx: GuardContext) -> None:
        started = time.perf_counter()
        assert_denied(decide("Read", {"file_path": "a/" * 500_000}, ctx))
        assert_denied(decide("Grep", {"path": "a" * 1_000_000}, ctx))
        assert time.perf_counter() - started < 1.0

    def test_경로가_2048자를_넘으면_거부한다(self, ctx: GuardContext) -> None:
        # `./`는 정규화하면 사라지므로 상한이 없으면 허용될 입력이다(이름 길이 오류도 PATH_MAX도
        # 걸리지 않는다). 그래서 이 거부는 길이 상한 때문이다.
        dots = "./" * 1100
        assert len("wiki/" + dots + "x.md") > 2048
        assert_allowed(decide("Read", {"file_path": "wiki/" + "./" * 50 + "x.md"}, ctx))
        assert_denied(decide("Read", {"file_path": "wiki/" + dots + "x.md"}, ctx))
        assert_denied(decide("Write", {"file_path": "wiki/" + dots + "x.md"}, ctx))
        assert_denied(decide("Grep", {"pattern": "x", "path": dots}, ctx))
        assert_denied(decide("Glob", {"pattern": "*", "path": dots}, ctx))
        assert_denied(decide("Glob", {"pattern": dots + "*"}, ctx))

    def test_2048자_이하의_깊은_경로는_허용한다(self, ctx: GuardContext) -> None:
        # OS의 PATH_MAX(macOS 1024)를 넘지 않는 깊이로 한다.
        ok = "wiki/" + "a/" * 400 + "x.md"
        assert len(ok) <= 2048
        assert_allowed(decide("Read", {"file_path": ok}, ctx))
        assert_allowed(decide("Write", {"file_path": ok}, ctx))

    def test_이름이_너무_길어_lstat이_실패해도_거부로_닫는다(self, ctx: GuardContext) -> None:
        assert_denied(decide("Write", {"file_path": "wiki/" + "a" * 300 + ".md"}, ctx))


# ---------------------------------------------------------------- CLI Glob 기준 폴더


class TestCliGlobBase:
    """번들 CLI는 절대 패턴을 `* ? [ {` 중 처음 나오는 글자에서 자르고, 그 앞의 마지막 `/`까지를
    검색 폴더로 쓴다(`]`와 `}`는 멈추지 않고 백슬래시는 구분자가 아니다).
    글롭 문자가 없으면 부모 폴더다."""

    def test_규칙_표(self) -> None:
        table = {
            "/r/data/wiki/**/*.md": "/r/data/wiki",
            "/r/data\\-evil*/**": "/r",
            "/r/data\\x/*": "/r/data\\x",
            "/r/data/a]/**": "/r/data/a]",
            "/r/data/b}/**": "/r/data/b}",
            "/r/data/{a,b}/*": "/r/data",
            "/r/data/file.md": "/r/data",
            "/r/data/": "/r",
            "/r/data//": "/r",
            "/r/": "/",
            "/r//": "/",
            "/r//data": "/r/",
            "/r/data/..  //*": "/r/data/..  /",
            "/r/data//*": "/r/data/",
            "/r/data/*.md": "/r/data",
            "/r/data/pa?th/x": "/r/data",
            "/r/data/a[b]/x": "/r/data",
            "/**": "/",
            "/*": "/",
            "/file": "/",
            "/r/**": "/r",
            "/r": "/",
        }
        for pattern, base in table.items():
            assert cli_glob_base(pattern) == base, pattern

    def test_형제_폴더로_빠지는_역슬래시_패턴은_거부한다(
        self, ctx: GuardContext, root: Path, data: Path
    ) -> None:
        write_tree(root, {"data-evil/x.md": "evil"})
        assert_denied(decide("Glob", {"pattern": f"{data}\\-evil*/**"}, ctx))
        assert_denied(decide("Glob", {"pattern": f"{data}\\-evil*"}, ctx))

    def test_정당한_절대_패턴은_허용한다(self, ctx: GuardContext, data: Path, repos: Path) -> None:
        assert_allowed(decide("Glob", {"pattern": f"{data}/wiki/**/*.md"}, ctx))
        assert_allowed(decide("Glob", {"pattern": f"{repos}/**/*.kt"}, ctx))
        assert_allowed(decide("Glob", {"pattern": f"{repos}/worker/src/*.kt"}, ctx))
        assert_allowed(decide("Glob", {"pattern": f"{data}/{{wiki,raw}}/**/*.md"}, ctx))

    def test_글롭_문자가_없는_패턴은_부모_폴더도_허용_안이어야_한다(
        self, ctx: GuardContext, root: Path, data: Path, repos: Path
    ) -> None:
        # 루트 자신: CLI는 루트의 부모(root)를 검색하므로 거부.
        assert_denied(decide("Glob", {"pattern": str(repos)}, ctx))
        assert_denied(decide("Glob", {"pattern": str(data)}, ctx))
        # 루트 안의 폴더·파일: 부모가 루트 안이므로 허용.
        assert_allowed(decide("Glob", {"pattern": str(data / "wiki")}, ctx))
        assert_allowed(decide("Glob", {"pattern": str(repos / "worker" / "src" / "A.kt")}, ctx))
        # 부모가 허용된 읽기 루트이면 루트 자신도 허용.
        wide = GuardContext(cwd=data, read_roots=(root,), write_roots=())
        assert_allowed(decide("Glob", {"pattern": str(repos)}, wide))

    def test_괄호_문자가_든_이름의_링크로_밖에_나가면_거부한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        os.symlink(outside, data / "a]")
        os.symlink(outside, data / "b}")
        assert_denied(decide("Glob", {"pattern": f"{data}/a]/**"}, ctx))
        assert_denied(decide("Glob", {"pattern": f"{data}/b}}/**"}, ctx))
        assert_denied(decide("Glob", {"pattern": f"{data}/a]/*.txt"}, ctx))

    def test_nul이_든_절대_패턴은_거부한다(self, ctx: GuardContext, data: Path) -> None:
        assert_denied(decide("Glob", {"pattern": f"{data}/wiki/\x00*"}, ctx))
        assert_denied(decide("Glob", {"pattern": f"{data}\x00/wiki/*"}, ctx))

    def test_상대_패턴은_기준이_검색_폴더라_그대로_허용한다(self, ctx: GuardContext) -> None:
        assert_allowed(decide("Glob", {"pattern": "wiki/a]/**"}, ctx))
        assert_allowed(decide("Glob", {"pattern": "wiki/**/*.md"}, ctx))


# ---------------------------------------------------------------- NFC 정규화


class TestNormalization:
    NFC = "wiki/한글.md"
    NFD = unicodedata.normalize("NFD", "wiki/한글.md")
    KELVIN = "wiki/\u212a.md"  # NFC로 바뀌면 'K'

    def test_전제_두_표기는_다르다(self) -> None:
        assert self.NFC != self.NFD
        assert unicodedata.is_normalized("NFC", self.NFC)
        assert not unicodedata.is_normalized("NFC", self.NFD)
        assert unicodedata.normalize("NFC", self.KELVIN) == "wiki/K.md"

    def test_분해형_경로는_모든_도구에서_거부한다(self, ctx: GuardContext) -> None:
        for tool in ("Read", "Edit", "Write"):
            assert_denied(decide(tool, {"file_path": self.NFD}, ctx), tool)
        assert_denied(decide("Grep", {"pattern": "x", "path": self.NFD}, ctx))
        assert_denied(decide("Glob", {"pattern": "*", "path": self.NFD}, ctx))
        assert_denied(decide("Glob", {"pattern": self.NFD + "*"}, ctx))
        assert_denied(decide("Glob", {"pattern": "wiki/" + self.NFD[5:-3] + "*.md"}, ctx))

    def test_조합형_경로는_허용한다(self, ctx: GuardContext, data: Path) -> None:
        assert_allowed(decide("Read", {"file_path": self.NFC}, ctx))
        assert_allowed(decide("Write", {"file_path": self.NFC}, ctx))
        assert_allowed(decide("Edit", {"file_path": str(data / self.NFC)}, ctx))
        assert_allowed(decide("Grep", {"pattern": "x", "path": "wiki/한글"}, ctx))
        assert_allowed(decide("Glob", {"pattern": "wiki/한글*.md"}, ctx))
        assert_allowed(decide("Glob", {"pattern": "*", "path": "wiki/한글"}, ctx))

    def test_켈빈_기호_같은_호환_문자는_거부한다(self, ctx: GuardContext) -> None:
        for tool in ("Read", "Edit", "Write"):
            assert_denied(decide(tool, {"file_path": self.KELVIN}, ctx), tool)
        assert_denied(decide("Grep", {"pattern": "x", "path": self.KELVIN}, ctx))
        assert_denied(decide("Glob", {"pattern": self.KELVIN}, ctx))

    def test_짝_없는_서로게이트는_예외_없이_거부한다(self, ctx: GuardContext) -> None:
        for bad in ("wiki/\udcff.md", "wiki/\ud800.md", "\udcff"):
            for tool in ("Read", "Edit", "Write"):
                assert_denied(decide(tool, {"file_path": bad}, ctx), f"{tool} {bad!r}")
            assert_denied(decide("Grep", {"pattern": "x", "path": bad}, ctx))
            assert_denied(decide("Glob", {"pattern": "*", "path": bad}, ctx))
            assert_denied(decide("Glob", {"pattern": bad}, ctx))

    def test_거부_사유가_정규화를_언급한다(self, ctx: GuardContext) -> None:
        assert "정규화" in deny_reason(decide("Read", {"file_path": self.NFD}, ctx))
        assert "정규화" in deny_reason(decide("Read", {"file_path": "wiki/\udcff"}, ctx))

    def test_분해형_철자는_파일시스템과_무관하게_정규화_때문에_거부한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        os.symlink(outside, data / "wiki" / "한글")  # NFC 이름의 링크
        nfd = unicodedata.normalize("NFD", "wiki/한글/secret.txt")
        # 리눅스에서는 NFD 이름이 존재하지 않아 링크 검사를 건너뛰게 된다. macOS에서도 같은 이유로
        # 거부되는지 사유로 확인한다(링크 때문이 아니라 정규화 때문이어야 한다).
        for d in (
            decide("Read", {"file_path": nfd}, ctx),
            decide("Write", {"file_path": nfd}, ctx),
            decide("Grep", {"pattern": "x", "path": nfd}, ctx),
            decide("Glob", {"pattern": "*", "path": nfd}, ctx),
            decide("Glob", {"pattern": unicodedata.normalize("NFD", "wiki/한글/*")}, ctx),
            decide("Glob", {"pattern": unicodedata.normalize("NFD", "wiki/한글/") + "*"}, ctx),
        ):
            assert "정규화" in deny_reason(d)
        # NFC 철자는 링크 검사에 걸려 거부된다.
        assert_denied(decide("Read", {"file_path": "wiki/한글/secret.txt"}, ctx))
        assert_denied(decide("Write", {"file_path": "wiki/한글/a.md"}, ctx))


class TestSyntaxExtras:
    def test_공백만_있는_grep_path는_거부한다(self, ctx: GuardContext) -> None:
        assert_denied(decide("Grep", {"pattern": "x", "path": "   "}, ctx))
        assert_denied(decide("Glob", {"pattern": "*", "path": "   "}, ctx))

    def test_물결표와_공백_거부_사유에_틸데가_있다(self, ctx: GuardContext) -> None:
        for p in ("~/.ssh/id_rsa", " /etc/passwd", "wiki/index.md\n"):
            reason = deny_reason(decide("Read", {"file_path": p}, ctx))
            assert "~" in reason and "공백" in reason, reason
        assert "~" in deny_reason(decide("Grep", {"pattern": "x", "path": "   "}, ctx))


# ------------------------------------------------------- CLI 검색 폴더 다듬기(trim·normalize)

# JS `trim()`이 자르는 문자는 정확히 25개다(U+180E, U+200B는 자르지 않는다).
JS_SPACES = [
    *(chr(c) for c in range(0x09, 0x0E)),
    " ",
    chr(0xA0),
    chr(0x1680),
    *(chr(c) for c in range(0x2000, 0x200B)),
    chr(0x2028),
    chr(0x2029),
    chr(0x202F),
    chr(0x205F),
    chr(0x3000),
    chr(0xFEFF),
]
NOT_TRIMMED = [chr(0x180E), chr(0x200B), chr(0x200C), chr(0x2060), chr(0x85)]


class TestCliSearchDir:
    """CLI는 기준 폴더를 먼저 자르고(`Ano`) 그 뒤에 `trim → normalize → NFC` 한다(`Ye`).
    글롭 문자가 있으면 마지막 `/` 앞까지(슬래시 연속은 보존), 없으면 `path.dirname`(마지막 슬래시
    하나만 지움)이다. 가드의 `cli_glob_search_dir`은 trim 전에 끝 슬래시를 모두 지우는 보수적인
    근사라서(안전, 이상한 이름은 더 거부할 수 있음) 실제 CLI와 다르게 `..`를 접는 경우도 있다."""

    def test_검색_폴더_표(self) -> None:
        table = {
            "/r/data/.. /*": "/r",
            "/r/data/..\t/**": "/r",
            "/r/data/.. /x.md": "/r",
            # 가드의 보수적 값. 실제 CLI의 기준 폴더는 `/r/data/..\x0c/`이고 trim이 듣지 않아
            # `/r/data/..\x0c`(그런 이름의 폴더)이지만, 가드는 더 넓게 `/r`까지 본다.
            "/r/data/..\x0c//*": "/r",
            "/r/data/..\x0c//b}": "/r",
            "/r/data/wiki /**": "/r/data/wiki",
            "/r/data//wiki/**": "/r/data/wiki",
            "/r/data/./wiki/**": "/r/data/wiki",
            "//r/data/**": "/r/data",
            "/r/data/": "/r",
            "/r/data/wiki/**/*.md": "/r/data/wiki",
            "/**": "/",
            "/r/../../**": "/",
        }
        for pattern, expected in table.items():
            assert cli_glob_search_dir(pattern) == expected, repr(pattern)
        for space in JS_SPACES:
            assert cli_glob_search_dir(f"/r/data/..{space}/*") == "/r", repr(space)
            assert cli_glob_search_dir(f"/r/data{space}/*") == "/r/data", repr(space)

    def test_끝_공백이_붙은_점점으로_부모를_훑지_못한다(
        self, ctx: GuardContext, data: Path, repos: Path
    ) -> None:
        for space in JS_SPACES:
            for pattern in (
                f"{data}/..{space}/*",
                f"{data}/..{space}/**",
                f"{data}/..{space}/x.md",
                f"{repos}/..{space}/**",
            ):
                assert_denied(decide("Glob", {"pattern": pattern}, ctx), repr(pattern))

    def test_끝_공백이_붙은_링크_이름으로_밖에_나가지_못한다(
        self, ctx: GuardContext, data: Path, outside: Path
    ) -> None:
        os.symlink(outside, data / "link")
        for space in JS_SPACES:
            assert_denied(decide("Glob", {"pattern": f"{data}/link{space}/*"}, ctx), repr(space))
        # 이름 자체에 끝 공백이 있는 링크(공백 없는 이름은 링크가 아니다).
        os.symlink(outside, data / "wiki" / "trail ")
        assert_denied(decide("Glob", {"pattern": f"{data}/wiki/trail /*"}, ctx))
        # 리터럴 패턴 자체가 밖을 가리키는 링크여도 거부한다.
        os.symlink(outside, data / "wiki" / "b}")
        assert_denied(decide("Glob", {"pattern": f"{data}/wiki/b}}"}, ctx))

    def test_끝_슬래시_패턴은_루트의_부모를_훑으므로_거부한다(
        self, ctx: GuardContext, data: Path, repos: Path
    ) -> None:
        for pattern in (f"{data}/", f"{data}//", f"{repos}/", f"{repos}///", "/"):
            assert_denied(decide("Glob", {"pattern": pattern}, ctx), pattern)

    def test_정당한_패턴은_그대로_허용한다(
        self, ctx: GuardContext, data: Path, repos: Path
    ) -> None:
        for pattern in (
            f"{data}/wiki/**/*.md",
            f"{repos}/**/*.kt",
            f"{data}/**",
            f"{data}/wiki/",
            f"{data}/wiki/**",
            f"{data}/wiki//**",
            f"{data}/./wiki/*.md",
            f"{repos}/worker/src/",
        ):
            assert_allowed(decide("Glob", {"pattern": pattern}, ctx))

    def test_이름_중간의_공백은_허용한다(self, ctx: GuardContext, data: Path) -> None:
        assert_allowed(decide("Glob", {"pattern": f"{data}/wiki/my file/**"}, ctx))
        assert_allowed(decide("Glob", {"pattern": f"{data}/wiki/a b/*.md"}, ctx))

    def test_js_공백은_정확히_25개이고_가드_상수와_같다(self) -> None:
        assert len(set(JS_SPACES)) == 25
        assert set(path_guard._JS_WHITESPACE) == set(JS_SPACES)
        assert len(path_guard._JS_WHITESPACE) == 25
        for ch in NOT_TRIMMED:
            assert ch not in path_guard._JS_WHITESPACE, hex(ord(ch))

    def test_trim되지_않는_문자는_공백으로_보지_않는다(self, ctx: GuardContext, data: Path) -> None:
        for ch in NOT_TRIMMED:
            assert cli_glob_search_dir(f"/r/data/..{ch}/*") == f"/r/data/..{ch}"
            # 그런 이름의 폴더일 뿐이라 루트 안이면 허용된다(`..`로 접히지 않는다).
            assert_allowed(decide("Glob", {"pattern": f"{data}/..{ch}/*"}, ctx))

    def test_가드_상수에서_어떤_문자를_빼도_실패한다(self, ctx: GuardContext, data: Path) -> None:
        original = path_guard._JS_WHITESPACE
        try:
            for ch in JS_SPACES:
                path_guard._JS_WHITESPACE = original.replace(ch, "")
                # 모든 25자는 검색 폴더 계산에서 바로 드러난다.
                assert cli_glob_search_dir(f"/r/data/..{ch}/*") != "/r", hex(ord(ch))
                # U+2000/U+2001은 NFC로 U+2002/U+2003이 되므로 어차피 NFC 검사에서 거부된다.
                if unicodedata.is_normalized("NFC", ch):
                    d = decide("Glob", {"pattern": f"{data}/..{ch}/*"}, ctx)
                    assert isinstance(d, Allow), f"{ord(ch):#x}를 빼도 거부됨(테스트가 무력)"
        finally:
            path_guard._JS_WHITESPACE = original
        assert_denied(decide("Glob", {"pattern": f"{data}/.. /*"}, ctx))

    def test_무작위_패턴으로_cli_검색_폴더가_밖이면_거부한다(
        self, ctx: GuardContext, root: Path, data: Path, repos: Path, outside: Path
    ) -> None:
        os.symlink(outside, data / "link")
        os.symlink(outside, data / "link ")
        os.symlink(outside, data / "a]")
        os.symlink(outside, data / "wiki" / "b}")
        os.symlink(outside, data / "wiki" / "name ")
        roots = [os.path.realpath(r) for r in (ctx.cwd, *ctx.read_roots)]
        trim_set = "".join(JS_SPACES)

        def inside(path: str) -> bool:
            real = Path(os.path.realpath(path))
            return any(real.is_relative_to(r) for r in roots)

        def node_dirname(s: str) -> str:
            """Node `path.posix.dirname`: 끝 슬래시를 모두 무시하고 마지막 구성요소 앞의
            슬래시 *하나*만 지운다(`/a//b` -> `/a/`)."""
            stripped = s.rstrip("/")
            if stripped == "":
                return "/"
            idx = stripped.rfind("/")
            if idx == -1:
                return "."
            return stripped[:idx] or "/"

        def emulate(pattern: str) -> list[str]:
            """실제 CLI 규칙: 글롭 문자 앞 마지막 `/`까지 자르거나(슬래시 연속 보존) dirname 한 뒤
            JS trim, Node normalize(`//`·`.`·`..` 접기), NFC. 글롭 문자가 없으면 리터럴도 본다."""
            cut = next((i for i, c in enumerate(pattern) if c in "*?[{"), None)
            if cut is None:
                base = node_dirname(pattern)
                literal = [pattern.rstrip("/") or "/"]
            else:
                base = pattern[: pattern.rfind("/", 0, cut)] or "/"
                literal = []
            normalized = posixpath.normpath(base.strip(trim_set))
            if normalized.startswith("//"):  # Node의 normalize는 맨 앞 `//`도 `/`로 접는다
                normalized = "/" + normalized.lstrip("/")
            return [unicodedata.normalize("NFC", normalized), *literal]

        rng = random.Random(20261007)
        starts = ["/", str(root), str(data), str(repos), str(data / "wiki"), str(outside)]
        atoms = [
            "..", ".", "wiki", "x.md", "link", "link ", "a]", "b}", "name ", "worker", "data",
            "*", "?", "[", "{", "]", "}", "\\", "/", "//", ",", "**", "*.md",
            *JS_SPACES,
        ]  # fmt: skip
        outside_cases = 0
        inside_cases = 0
        inside_allowed = 0
        for _ in range(3000):
            pattern = rng.choice(starts) + "".join(
                rng.choice(["/", "", "/"]) + rng.choice(atoms) for _ in range(rng.randint(0, 6))
            )
            if not pattern.startswith("/") or "\x00" in pattern:
                continue
            decision = decide("Glob", {"pattern": pattern}, ctx)
            if all(inside(t) for t in emulate(pattern)):
                inside_cases += 1
                inside_allowed += isinstance(decision, Allow)
                continue
            outside_cases += 1
            assert isinstance(decision, Deny), f"CLI는 밖을 훑는데 허용됨: {pattern!r}"
        # 무작위 입력이 한쪽으로 쏠리지 않았는지 확인한다.
        assert outside_cases > 200
        assert inside_cases > 100
        # 과잉 거부 점검(전부 거부하는 가드를 잡는다). 실측은 안쪽 1056건 중 404건(38%)을
        # 허용했고 나머지는 `..` 구간·앞뒤 공백·글롭 접두 같은 정당한 이유로 거부된다.
        # 그래서 하한을 25%로 둔다.
        assert inside_allowed >= inside_cases * 0.25, (inside_allowed, inside_cases)

    def test_상대_글롭의_검색_루트가_밖이면_패턴이_안쪽이어도_거부한다(
        self, ctx: GuardContext, data: Path, outside: Path, repos: Path
    ) -> None:
        # CLI는 `path`(없으면 cwd)에서 ripgrep을 돌리고 패턴은 그 아래를 거르는 필터일 뿐이다.
        # `<outside>/in`이 루트 안을 가리키는 링크여도 `<outside>` 전체를 훑는다.
        os.symlink(data / "wiki", outside / "in")
        for pattern in ("in", "in/**", "in/*.md", "**/in"):
            assert_denied(decide("Glob", {"pattern": pattern, "path": str(outside)}, ctx), pattern)
            assert_denied(decide("Glob", {"pattern": pattern, "path": "../outside"}, ctx), pattern)
        # 절대 패턴이어도 검색 루트(path)가 밖이면 거부한다.
        assert_denied(decide("Glob", {"pattern": f"{data}/wiki/**", "path": str(outside)}, ctx))
        # 밖의 path에 무해한 패턴도 거부한다(원래도 그랬다).
        assert_denied(decide("Glob", {"pattern": "*.txt", "path": str(outside)}, ctx))
        # 정당한 상대 패턴은 그대로 허용한다.
        assert_allowed(decide("Glob", {"pattern": "**/*.md", "path": str(data / "wiki")}, ctx))
        assert_allowed(decide("Glob", {"pattern": "wiki/**/*.md"}, ctx))
        assert_allowed(decide("Glob", {"pattern": "**/*.kt", "path": str(repos)}, ctx))
        assert_allowed(decide("Glob", {"pattern": "*.kt", "path": "../repos/worker"}, ctx))
