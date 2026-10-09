"""데이터 사본 준비(G4·G9). 모든 저장소는 임시 폴더의 실제 bare 저장소(`file` 전송)다."""

import asyncio
import errno
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import CloneStateError, remote_url, rev_parse
from team_wiki_engine.sync.data_copy import prepare_data_copy, remove_stale_git_locks
from tests.helpers.git import commit_files, seed_bare

DATA_FILES = {
    ".gitignore": ".DS_Store\n",
    "wiki/a.md": "# A\n",
    "wiki/b.md": "# B\n",
}


def make_git(root: Path, *, git_bin: str = "git") -> GitClient:
    return GitClient(root, allowed_protocols="file", timeout_s=60.0, git_bin=git_bin)


async def seeded(tmp_dir: Path, name: str = "data") -> tuple[Path, GitClient]:
    bare = tmp_dir / f"{name}.git"
    work = await seed_bare(bare, tmp_dir / f"{name}-work", DATA_FILES)
    return bare, work


def partials(parent: Path) -> list[str]:
    return sorted(entry.name for entry in parent.iterdir() if ".partial-" in entry.name)


def write_script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class TestFirstRun:
    async def test_처음에는_clone하고_origin_main과_같다(self, tmp_dir: Path) -> None:
        bare, work = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "state" / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        assert (copy.cwd / "wiki" / "a.md").read_text(encoding="utf-8") == "# A\n"
        assert await rev_parse(copy, "HEAD") == await rev_parse(copy, "origin/main")
        assert await rev_parse(copy, "HEAD") == await rev_parse(work, "HEAD")
        assert (await copy.run(["config", "--get", "core.symlinks"])).text.strip() == "false"
        assert await remote_url(copy) == str(bare)
        assert partials(copy.cwd.parent) == []

    async def test_빈_폴더가_이미_있어도_clone한다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        root = tmp_dir / "data-sync"
        root.mkdir()
        copy = make_git(root)
        await prepare_data_copy(copy, str(bare), "main")
        assert (root / "wiki" / "a.md").is_file()
        assert partials(tmp_dir) == []

    async def test_원격이_앞서가면_두_번째_호출이_따라간다(self, tmp_dir: Path) -> None:
        bare, work = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        new_sha = await commit_files(work, {"wiki/c.md": "# C\n"}, "add c")
        await work.run(["push", "-q", "origin", "main"])
        await prepare_data_copy(copy, str(bare), "main")
        assert await rev_parse(copy, "HEAD") == new_sha
        assert (copy.cwd / "wiki" / "c.md").is_file()

    async def test_브랜치가_main이_아니어도_된다(self, tmp_dir: Path) -> None:
        bare, work = await seeded(tmp_dir)
        await work.run(["push", "-q", "origin", "main:release"])
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "release")
        assert await rev_parse(copy, "HEAD") == await rev_parse(copy, "origin/release")
        assert (await copy.run(["rev-parse", "--abbrev-ref", "HEAD"])).text.strip() == "release"


class TestForcedSync:
    async def test_로컬_수정_새_파일_무시된_파일을_모두_지운다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        root = copy.cwd
        (root / "wiki" / "a.md").write_text("사람이 고침\n", encoding="utf-8")
        (root / "wiki" / "new.md").write_text("추적 안 됨\n", encoding="utf-8")
        (root / "wiki" / ".DS_Store").write_bytes(b"\x00\x01mac")
        (root / "wiki" / "sub").mkdir()
        (root / "wiki" / "sub" / "deep.md").write_text("x", encoding="utf-8")
        (root / "wiki" / "b.md").unlink()
        await copy.run(["add", "wiki/new.md"])
        await prepare_data_copy(copy, str(bare), "main")
        assert (root / "wiki" / "a.md").read_text(encoding="utf-8") == "# A\n"
        assert (root / "wiki" / "b.md").read_text(encoding="utf-8") == "# B\n"
        assert not (root / "wiki" / "new.md").exists()
        assert not (root / "wiki" / ".DS_Store").exists()
        assert not (root / "wiki" / "sub").exists()
        status = await copy.run(["status", "--porcelain=v1", "-uall", "--ignored=matching"])
        assert status.text == ""

    async def test_로컬에만_있는_커밋도_origin으로_되돌린다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        await copy.run(["config", "user.name", "x"])
        await copy.run(["config", "user.email", "x@example.com"])
        await commit_files(copy, {"wiki/local.md": "로컬"}, "local only")
        await prepare_data_copy(copy, str(bare), "main")
        assert await rev_parse(copy, "HEAD") == await rev_parse(copy, "origin/main")
        assert not (copy.cwd / "wiki" / "local.md").exists()


class TestSymlinks:
    async def _commit_link(self, work: GitClient) -> None:
        os.symlink("a.md", work.cwd / "wiki" / "link.md")
        await work.run(["add", "-A"])
        await work.run(["commit", "-q", "-m", "add link"])
        await work.run(["push", "-q", "origin", "main"])

    async def test_커밋된_링크는_일반_파일로_나온다(self, tmp_dir: Path) -> None:
        bare, work = await seeded(tmp_dir)
        await self._commit_link(work)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        link = copy.cwd / "wiki" / "link.md"
        assert not link.is_symlink()
        assert stat.S_ISREG(os.lstat(link).st_mode)
        assert link.read_text(encoding="utf-8") == "a.md"  # 링크 대상 문자열을 담은 일반 파일

    async def test_설정_없이_만든_예전_clone의_링크를_일반_파일로_바꾼다(
        self, tmp_dir: Path
    ) -> None:
        bare, work = await seeded(tmp_dir)
        await self._commit_link(work)
        old = make_git(tmp_dir / "old-clone")
        await old.at(tmp_dir).run(["clone", "-q", "--", str(bare), str(old.cwd)])
        link = old.cwd / "wiki" / "link.md"
        assert link.is_symlink()  # 예전 방식 clone은 실제 링크로 풀려 있다
        await prepare_data_copy(old, str(bare), "main")
        assert (await old.run(["config", "--get", "core.symlinks"])).text.strip() == "false"
        assert not link.is_symlink()
        assert stat.S_ISREG(os.lstat(link).st_mode)
        assert link.read_text(encoding="utf-8") == "a.md"
        assert await rev_parse(old, "HEAD") == await rev_parse(old, "origin/main")

    async def test_추적되지_않는_링크도_지운다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        outside = tmp_dir / "outside"
        outside.mkdir()
        os.symlink(outside, copy.cwd / "wiki" / "escape")
        await prepare_data_copy(copy, str(bare), "main")
        assert not (copy.cwd / "wiki" / "escape").exists()
        assert not (copy.cwd / "wiki" / "escape").is_symlink()
        assert outside.is_dir()


class TestRemoteUrl:
    async def test_설정의_URL이_바뀌면_원격도_따라_바뀐다(self, tmp_dir: Path) -> None:
        old_bare, _ = await seeded(tmp_dir, "old")
        new_bare, new_work = await seeded(tmp_dir, "new")
        new_sha = await commit_files(new_work, {"wiki/moved.md": "이사"}, "moved")
        await new_work.run(["push", "-q", "origin", "main"])
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(old_bare), "main")
        assert await remote_url(copy) == str(old_bare)
        await prepare_data_copy(copy, str(new_bare), "main")
        assert await remote_url(copy) == str(new_bare)
        assert await rev_parse(copy, "HEAD") == new_sha

    async def test_같은_URL이면_원격_설정을_건드리지_않는다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        before = (copy.cwd / ".git" / "config").read_bytes()
        await prepare_data_copy(copy, str(bare), "main")
        assert (copy.cwd / ".git" / "config").read_bytes() == before


class TestRefusals:
    async def test_dot_git이_링크이면_CloneStateError이다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        elsewhere = make_git(tmp_dir / "elsewhere")
        await prepare_data_copy(elsewhere, str(bare), "main")
        root = tmp_dir / "data-sync"
        root.mkdir()
        os.symlink(elsewhere.cwd / ".git", root / ".git")
        with pytest.raises(CloneStateError):
            await prepare_data_copy(make_git(root), str(bare), "main")
        assert (root / ".git").is_symlink()

    async def test_사본_폴더_자체가_링크이면_CloneStateError이다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        elsewhere = make_git(tmp_dir / "elsewhere")
        await prepare_data_copy(elsewhere, str(bare), "main")
        root = tmp_dir / "data-sync"
        os.symlink(elsewhere.cwd, root)
        with pytest.raises(CloneStateError):
            await prepare_data_copy(make_git(root), str(bare), "main")

    async def test_링크인_빈_폴더도_따라가지_않는다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        empty = tmp_dir / "empty-target"
        empty.mkdir()
        root = tmp_dir / "data-sync"
        os.symlink(empty, root)
        with pytest.raises(CloneStateError):
            await prepare_data_copy(make_git(root), str(bare), "main")
        assert os.listdir(empty) == []

    async def test_git_없이_채워진_폴더는_건드리지_않고_CloneStateError이다(
        self, tmp_dir: Path
    ) -> None:
        bare, _ = await seeded(tmp_dir)
        root = tmp_dir / "data-sync"
        root.mkdir()
        (root / "mine.txt").write_text("소중", encoding="utf-8")
        with pytest.raises(CloneStateError):
            await prepare_data_copy(make_git(root), str(bare), "main")
        assert (root / "mine.txt").read_text(encoding="utf-8") == "소중"
        assert partials(tmp_dir) == []

    async def test_원격에_base_브랜치가_없으면_GitError이다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        with pytest.raises(GitError):
            await prepare_data_copy(copy, str(bare), "nope")

    async def test_안전하지_않은_base는_git을_부르기_전에_ValueError이다(
        self, tmp_dir: Path
    ) -> None:
        bare, _ = await seeded(tmp_dir)
        root = tmp_dir / "data-sync"
        for bad in ("--upload-pack=x", "", "a..b", "main\n", "refs/heads/main"):
            with pytest.raises(ValueError):
                await prepare_data_copy(make_git(root), str(bare), bad)
        assert not root.exists()
        assert partials(tmp_dir) == []

    async def test_안전하지_않은_URL은_ValueError이고_아무것도_만들지_않는다(
        self, tmp_dir: Path
    ) -> None:
        root = tmp_dir / "data-sync"
        with pytest.raises(ValueError):
            await prepare_data_copy(make_git(root), "--upload-pack=touch /tmp/x", "main")
        assert not root.exists()
        assert partials(tmp_dir) == []


class TestInterruptedClone:
    async def test_clone이_실패하면_사본_폴더도_조각도_남지_않는다(self, tmp_dir: Path) -> None:
        root = tmp_dir / "data-sync"
        with pytest.raises(GitError):
            await prepare_data_copy(make_git(root), str(tmp_dir / "no-such.git"), "main")
        assert not root.exists()
        assert partials(tmp_dir) == []
        # 같은 자리에서 다음 실행이 정상으로 돈다(CloneStateError가 영원히 나지 않는다).
        bare, _ = await seeded(tmp_dir)
        await prepare_data_copy(make_git(root), str(bare), "main")
        assert (root / "wiki" / "a.md").is_file()

    async def test_clone_도중_취소되어도_사본_폴더가_반쯤_차지_않는다(self, tmp_dir: Path) -> None:
        fake = write_script(
            tmp_dir / "slow-git",
            'for last; do :; done\nmkdir -p "$last/.git"\necho half > "$last/junk"\nsleep 60\n',
        )
        root = tmp_dir / "data-sync"
        task = asyncio.ensure_future(
            prepare_data_copy(make_git(root, git_bin=fake), str(tmp_dir / "x.git"), "main")
        )
        for _ in range(200):  # 가짜 clone이 조각 폴더를 만들 때까지
            if partials(tmp_dir):
                break
            await asyncio.sleep(0.05)
        assert partials(tmp_dir), "가짜 clone이 조각 폴더를 만들지 않았다"
        assert not root.exists()  # 조각은 따로 만들고 끝나야 이름을 바꾼다
        task.cancel()
        async with asyncio.timeout(10):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not root.exists()
        assert partials(tmp_dir) == []
        bare, _ = await seeded(tmp_dir)
        await prepare_data_copy(make_git(root), str(bare), "main")
        assert (root / "wiki" / "a.md").is_file()

    async def test_죽은_프로세스가_남긴_조각은_다음_실행이_치운다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        stale = tmp_dir / f".data-sync.partial-{dead.pid}-deadbeef"
        (stale / ".git").mkdir(parents=True)
        (stale / "junk").write_text("반쯤", encoding="utf-8")
        outside = tmp_dir / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("남아야 함", encoding="utf-8")
        os.symlink(outside, stale / "link-out")  # 지울 때 링크를 따라가면 바깥이 지워진다
        link_partial = tmp_dir / f".data-sync.partial-{dead.pid}-cafe0001"
        os.symlink(outside, link_partial)  # 조각 이름 자체가 링크
        await prepare_data_copy(make_git(tmp_dir / "data-sync"), str(bare), "main")
        assert partials(tmp_dir) == []
        assert (outside / "keep.txt").read_text(encoding="utf-8") == "남아야 함"

    async def test_살아_있는_다른_프로세스의_조각은_건드리지_않는다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            theirs = tmp_dir / f".data-sync.partial-{alive.pid}-abcd1234"
            theirs.mkdir()
            (theirs / "work").write_text("진행 중", encoding="utf-8")
            await prepare_data_copy(make_git(tmp_dir / "data-sync"), str(bare), "main")
            assert (theirs / "work").read_text(encoding="utf-8") == "진행 중"
        finally:
            alive.send_signal(signal.SIGKILL)
            alive.wait()

    async def test_다른_이름_조각이나_모양이_다른_것은_지우지_않는다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        other = tmp_dir / ".data.partial-1-abcd"  # 다른 사본(data)의 조각
        other.mkdir()
        odd = tmp_dir / ".data-sync.partial-notapid"
        odd.mkdir()
        mine_file = tmp_dir / "data-sync.partial-1-aa"  # 점으로 시작하지 않음
        mine_file.write_text("x", encoding="utf-8")
        await prepare_data_copy(make_git(tmp_dir / "data-sync"), str(bare), "main")
        assert other.is_dir() and odd.is_dir() and mine_file.is_file()

    async def test_터무니없이_큰_pid_이름도_던지지_않고_치운다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        huge = tmp_dir / f".data-sync.partial-{'9' * 40}-abcd"
        huge.mkdir()
        await prepare_data_copy(make_git(tmp_dir / "data-sync"), str(bare), "main")
        assert not huge.exists()


def slow_on(tmp_dir: Path, subcommand: str, seconds: int) -> str:
    """`subcommand` 인자가 보이면 `seconds`초 자고 진짜 git을 부르는 가짜 git."""
    real_git = shutil.which("git")
    assert real_git is not None
    return write_script(
        tmp_dir / f"slow-{subcommand}-git",
        f'for a; do\n  if [ "$a" = {subcommand} ]; then sleep {seconds}; fi\ndone\n'
        f'exec "{real_git}" "$@"\n',
    )


async def advance(work: GitClient) -> str:
    sha = await commit_files(work, {"wiki/c.md": "# C\n"}, "add c")
    await work.run(["push", "-q", "origin", "main"])
    return sha


def lock_files(root: Path) -> list[Path]:
    return list((root / ".git").rglob("*.lock"))


class TestInterruptedMutation:
    async def test_fetch_도중_취소는_바로_전달되고_다음_준비가_성공한다(
        self, tmp_dir: Path
    ) -> None:
        bare, work = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        new_sha = await advance(work)
        slow_copy = make_git(copy.cwd, git_bin=slow_on(tmp_dir, "fetch", 30))
        task = asyncio.ensure_future(prepare_data_copy(slow_copy, str(bare), "main"))
        await asyncio.sleep(0.4)  # fetch가 자는 동안
        started = time.monotonic()
        task.cancel()
        async with asyncio.timeout(10):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert time.monotonic() - started < 2
        # 죽은 fetch가 ref 잠금을 남겼어도 다음 준비가 회복한다.
        (copy.cwd / ".git" / "refs" / "remotes" / "origin" / "main.lock").write_text("")
        await prepare_data_copy(copy, str(bare), "main")
        assert await rev_parse(copy, "HEAD") == new_sha
        assert lock_files(copy.cwd) == []

    @pytest.mark.parametrize("subcommand", ["checkout", "clean"])
    async def test_checkout이나_clean_도중_취소돼도_reset은_끝까지_돈다(
        self, tmp_dir: Path, subcommand: str
    ) -> None:
        bare, work = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        new_sha = await advance(work)
        (copy.cwd / "wiki" / "junk.md").write_text("지워져야 함", encoding="utf-8")
        slow_copy = make_git(copy.cwd, git_bin=slow_on(tmp_dir, subcommand, 1))
        task = asyncio.ensure_future(prepare_data_copy(slow_copy, str(bare), "main"))
        await asyncio.sleep(0.5)
        task.cancel()
        async with asyncio.timeout(30):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await rev_parse(copy, "HEAD") == new_sha
        assert lock_files(copy.cwd) == []
        assert not (copy.cwd / "wiki" / "junk.md").exists()
        status = await copy.run(["status", "--porcelain=v1", "-uall", "--ignored=matching"])
        assert status.text == ""


STALE_LOCKS = [
    "index.lock",
    "HEAD.lock",
    "config.lock",
    "packed-refs.lock",
    "shallow.lock",
    "refs/heads/main.lock",
    "refs/remotes/origin/main.lock",
]


class TestStaleGitLocks:
    @pytest.mark.parametrize("name", STALE_LOCKS)
    async def test_남은_잠금_파일이_있어도_준비가_성공한다(
        self, tmp_dir: Path, name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        bare, work = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        new_sha = await advance(work)
        (copy.cwd / ".git" / name).write_text("", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="team_wiki_engine"):
            await prepare_data_copy(copy, str(bare), "main")
        assert await rev_parse(copy, "HEAD") == new_sha
        assert str(tmp_dir) not in caplog.text

    async def test_회복하면_경고를_남기고_잠금_파일을_지운다(
        self, tmp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        (copy.cwd / ".git" / "index.lock").write_text("", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="team_wiki_engine"):
            await prepare_data_copy(copy, str(bare), "main")
        assert [r for r in caplog.records if r.levelno == logging.WARNING]
        assert lock_files(copy.cwd) == []
        assert str(tmp_dir) not in caplog.text

    async def test_링크인_잠금_파일은_따라가지도_지우지도_않는다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        victim = tmp_dir / "victim.txt"
        victim.write_text("소중", encoding="utf-8")
        os.symlink(victim, copy.cwd / ".git" / "index.lock")
        with pytest.raises(GitError):
            await prepare_data_copy(copy, str(bare), "main")
        assert victim.read_text(encoding="utf-8") == "소중"
        assert (copy.cwd / ".git" / "index.lock").is_symlink()

    async def test_폴더나_FIFO인_잠금_파일은_지우지_않는다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        (copy.cwd / ".git" / "index.lock").mkdir()
        os.mkfifo(copy.cwd / ".git" / "HEAD.lock")
        with pytest.raises(GitError):
            await prepare_data_copy(copy, str(bare), "main")
        assert (copy.cwd / ".git" / "index.lock").is_dir()
        assert stat.S_ISFIFO(os.lstat(copy.cwd / ".git" / "HEAD.lock").st_mode)

    async def test_다시_시도해도_실패하면_처음_오류를_던진다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        (copy.cwd / ".git" / "index.lock").write_text("", encoding="utf-8")
        with pytest.raises(GitError) as info:
            await prepare_data_copy(copy, str(bare), "nope")  # 원격에 없는 브랜치
        assert info.value.kind == "exit"
        assert str(tmp_dir) not in str(info.value)

    async def test_다시_시도가_다른_오류로_실패해도_처음_오류_객체를_던진다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        (copy.cwd / ".git" / "index.lock").write_text("", encoding="utf-8")
        errors = [GitError("처음", kind="exit", code=128), GitError("두 번째", kind="exit", code=1)]
        calls: list[int] = []

        async def failing(*_a: object) -> None:
            calls.append(1)
            raise errors[len(calls) - 1]

        monkeypatch.setattr("team_wiki_engine.sync.data_copy._sync_existing", failing)
        with pytest.raises(GitError) as info:
            await prepare_data_copy(copy, str(bare), "main")
        assert info.value is errors[0]
        assert len(calls) == 2

    async def test_시간_초과는_잠금을_지우지도_다시_시도하지도_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        lock = copy.cwd / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        calls: list[int] = []

        async def timing_out(*_a: object) -> None:
            calls.append(1)
            raise GitError("git fetch 시간 초과", kind="timeout")

        monkeypatch.setattr("team_wiki_engine.sync.data_copy._sync_existing", timing_out)
        with pytest.raises(GitError) as info:
            await prepare_data_copy(copy, str(bare), "main")
        assert info.value.kind == "timeout"
        assert len(calls) == 1
        assert lock.exists()

    async def test_지울_잠금이_없으면_한_번만_시도하고_경고도_없다(
        self,
        tmp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        calls: list[int] = []

        async def failing(*_a: object) -> None:
            calls.append(1)
            raise GitError("git fetch 실패", kind="exit", code=128)

        monkeypatch.setattr("team_wiki_engine.sync.data_copy._sync_existing", failing)
        with caplog.at_level(logging.WARNING, logger="team_wiki_engine"):
            with pytest.raises(GitError):
                await prepare_data_copy(copy, str(bare), "main")
        assert len(calls) == 1
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    async def test_git_오류가_아니면_잠금을_건드리지_않는다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        lock = copy.cwd / ".git" / "refs" / "heads" / "main.lock"
        lock.write_text("", encoding="utf-8")
        with pytest.raises(ValueError):
            await prepare_data_copy(copy, str(bare), "--bad")
        assert lock.exists()

    async def test_가드가_실패하면_잠금을_지우지_않는다_링크_폴더(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        real = make_git(tmp_dir / "real")
        await prepare_data_copy(real, str(bare), "main")
        lock = real.cwd / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        os.symlink(real.cwd, tmp_dir / "linked")
        assert remove_stale_git_locks(make_git(tmp_dir / "linked"), "main") == 0
        assert lock.exists()

    async def test_가드가_실패하면_잠금을_지우지_않는다_이상한_HEAD(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        lock = copy.cwd / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        (copy.cwd / ".git" / "HEAD").write_text("쓰레기\n", encoding="utf-8")
        assert remove_stale_git_locks(copy, "main") == 0
        assert lock.exists()

    async def test_조상이_링크인_ref_잠금은_따라가지_않는다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        outside = tmp_dir / "outside-heads"
        outside.mkdir()
        (outside / "main.lock").write_text("남아야 함", encoding="utf-8")
        heads = copy.cwd / ".git" / "refs" / "heads"
        shutil.rmtree(heads)
        os.symlink(outside, heads)
        assert remove_stale_git_locks(copy, "main") == 0
        assert (outside / "main.lock").read_text(encoding="utf-8") == "남아야 함"

    async def test_지운_개수와_일반_파일만_센다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        assert remove_stale_git_locks(copy, "main") == 0  # 지울 게 없다
        (copy.cwd / ".git" / "index.lock").write_text("", encoding="utf-8")
        assert remove_stale_git_locks(copy, "main") == 1
        assert not (copy.cwd / ".git" / "index.lock").exists()

    async def test_objects의_maintenance_lock도_일반_파일이면_지운다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        lock = copy.cwd / ".git" / "objects" / "maintenance.lock"
        lock.write_text("", encoding="utf-8")
        assert remove_stale_git_locks(copy, "main") == 1
        assert not lock.exists()

    async def test_링크인_maintenance_lock은_지우지_않는다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        copy = make_git(tmp_dir / "data-sync")
        await prepare_data_copy(copy, str(bare), "main")
        target = tmp_dir / "target.txt"
        target.write_text("남아야 함", encoding="utf-8")
        lock = copy.cwd / ".git" / "objects" / "maintenance.lock"
        os.symlink(target, lock)
        assert remove_stale_git_locks(copy, "main") == 0
        assert target.read_text(encoding="utf-8") == "남아야 함"


class TestRenameFailure:
    async def test_EBUSY면_사본_자리가_비어_있을_때_제자리에서_clone한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare, _ = await seeded(tmp_dir)
        root = tmp_dir / "data-sync"
        root.mkdir()

        def busy(src: object, dst: object) -> None:
            raise OSError(errno.EBUSY, "busy")

        monkeypatch.setattr(os, "rename", busy)
        await prepare_data_copy(make_git(root), str(bare), "main")
        assert (root / "wiki" / "a.md").is_file()
        assert partials(tmp_dir) == []

    async def test_제자리_clone이_실패하면_사본_자리를_다시_비운다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_dir / "data-sync"
        root.mkdir()

        def busy(src: object, dst: object) -> None:
            raise OSError(errno.EXDEV, "xdev")

        monkeypatch.setattr(os, "rename", busy)
        fake = write_script(
            tmp_dir / "fake-git",
            'for last; do :; done\nmkdir -p "$last/.git"\necho half > "$last/junk"\n'
            'case "$last" in *partial*) exit 0;; esac\nexit 1\n',
        )
        with pytest.raises(GitError):
            await prepare_data_copy(make_git(root, git_bin=fake), str(tmp_dir / "x.git"), "main")
        assert os.listdir(root) == []  # 반쯤 받은 조각이 사본 자리에 남지 않았다

    async def test_사본_자리가_그_사이_채워지면_CloneStateError이다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare, _ = await seeded(tmp_dir)
        root = tmp_dir / "data-sync"
        real_rename = os.rename

        def racing(src: str, dst: str) -> None:
            (Path(dst)).mkdir(exist_ok=True)
            (Path(dst) / "mine.txt").write_text("소중", encoding="utf-8")
            real_rename(src, dst)

        monkeypatch.setattr(os, "rename", racing)
        with pytest.raises(CloneStateError) as info:
            await prepare_data_copy(make_git(root), str(bare), "main")
        assert "채워짐" in str(info.value)
        assert str(tmp_dir) not in str(info.value)
        assert (root / "mine.txt").read_text(encoding="utf-8") == "소중"
        assert partials(tmp_dir) == []


class TestSweepEdges:
    async def test_접미어가_더_붙은_이름은_지우지_않는다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        odd = tmp_dir / f".data-sync.partial-{dead.pid}-abcd.bak"
        odd.mkdir()
        await prepare_data_copy(make_git(tmp_dir / "data-sync"), str(bare), "main")
        assert odd.is_dir()

    async def test_권한이_없는_다른_사용자의_프로세스_조각은_살아_있는_것으로_본다(
        self, tmp_dir: Path
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root는 pid 1에 신호를 보낼 수 있다")
        bare, _ = await seeded(tmp_dir)
        theirs = tmp_dir / ".data-sync.partial-1-abcd"  # pid 1: kill이 EPERM
        theirs.mkdir()
        await prepare_data_copy(make_git(tmp_dir / "data-sync"), str(bare), "main")
        assert theirs.is_dir()

    async def test_읽기_전용_하위_폴더가_있는_조각도_치운다(self, tmp_dir: Path) -> None:
        bare, _ = await seeded(tmp_dir)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        stale = tmp_dir / f".data-sync.partial-{dead.pid}-deadbeef"
        (stale / "ro" / "inner").mkdir(parents=True)
        (stale / "ro" / "file.txt").write_text("x", encoding="utf-8")
        (stale / "ro" / "inner" / "f").write_text("y", encoding="utf-8")
        (stale / "closed").mkdir()
        (stale / "closed" / "f").write_text("z", encoding="utf-8")
        outside = tmp_dir / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("남아야 함", encoding="utf-8")
        os.symlink(outside, stale / "ro" / "link-out")
        (stale / "ro" / "inner").chmod(0o500)
        (stale / "ro").chmod(0o500)
        (stale / "closed").chmod(0o000)
        try:
            await prepare_data_copy(make_git(tmp_dir / "data-sync"), str(bare), "main")
        finally:
            for path in (stale / "ro", stale / "ro" / "inner", stale / "closed"):
                if path.exists():
                    path.chmod(0o700)
        assert partials(tmp_dir) == []
        assert (outside / "keep.txt").read_text(encoding="utf-8") == "남아야 함"

    async def test_같은_프로세스에서_진행_중인_다른_호출의_조각은_건드리지_않는다(
        self, tmp_dir: Path
    ) -> None:
        bare, _ = await seeded(tmp_dir)
        fake = write_script(
            tmp_dir / "slow-git",
            'for last; do :; done\nmkdir -p "$last/.git"\necho half > "$last/junk"\nsleep 60\n',
        )
        root = tmp_dir / "data-sync"
        slow = asyncio.ensure_future(
            prepare_data_copy(make_git(root, git_bin=fake), str(tmp_dir / "x.git"), "main")
        )
        for _ in range(200):
            if partials(tmp_dir):
                break
            await asyncio.sleep(0.05)
        mine = partials(tmp_dir)
        assert len(mine) == 1
        # 같은 pid의 두 번째 호출은 진행 중인 조각을 지우지 않고 자기 일을 한다.
        await prepare_data_copy(make_git(root), str(bare), "main")
        assert partials(tmp_dir) == mine
        slow.cancel()
        async with asyncio.timeout(10):
            with pytest.raises(asyncio.CancelledError):
                await slow
        assert partials(tmp_dir) == []
        assert (root / "wiki" / "a.md").is_file()
