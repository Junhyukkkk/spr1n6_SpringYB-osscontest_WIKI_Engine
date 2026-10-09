"""코드 사본 동기화·코드 사본 시점 기록·봇용 기준 블록.

모든 저장소는 임시 폴더의 실제 bare 저장소다(`file` 전송만 허용).
"""

import asyncio
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from team_wiki_engine.config.schema import RepoConfig
from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import checkout_detached as real_checkout_detached
from team_wiki_engine.git.ops import disable_symlinks as real_disable_symlinks
from team_wiki_engine.git.ops import ensure_clone as real_ensure_clone
from team_wiki_engine.git.ops import remote_url, rev_parse
from team_wiki_engine.git.ops import set_remote_url as real_set_remote_url
from team_wiki_engine.metrics.record import STATE_FILES
from team_wiki_engine.sync.data_copy import remove_stale_git_locks
from team_wiki_engine.sync.repos import (
    CodeSnapshot,
    CodeSyncResult,
    basis_lines,
    read_code_snapshot,
    sync_code_repos,
    write_code_snapshot,
)
from team_wiki_engine.wiki.baseline import Baseline, RepoBaseline
from tests.helpers.git import commit_files, seed_bare


def make_git(root: Path) -> GitClient:
    return GitClient(root, allowed_protocols="file", timeout_s=60.0)


def active(name: str, url: str | Path, branch: str = "main") -> RepoConfig:
    return RepoConfig(name=name, url=str(url), branch=branch, status="active")


class TestSyncCodeRepos:
    async def test_active만_받고_실패한_저장소는_따로_모은다(self, tmp_dir: Path) -> None:
        work = await seed_bare(tmp_dir / "worker.git", tmp_dir / "w", {"src/A.kt": "class A"})
        head = await rev_parse(work, "HEAD")
        result = await sync_code_repos(
            repos=[
                active("worker", tmp_dir / "worker.git"),
                active("gone", tmp_dir / "nope.git"),
                RepoConfig(
                    name="old", url=str(tmp_dir / "worker.git"), branch="main", status="archived"
                ),
            ],
            repos_dir=tmp_dir / "repos",
            git=make_git(tmp_dir),
        )
        assert isinstance(result, CodeSyncResult)
        assert result.heads == {"worker": head}
        assert list(result.failures) == ["gone"]
        assert not (tmp_dir / "repos" / "old").exists()

    async def test_다시_부르면_새_커밋으로_맞춘다(self, tmp_dir: Path) -> None:
        work = await seed_bare(tmp_dir / "worker.git", tmp_dir / "w", {"src/A.kt": "class A"})
        repos = [active("worker", tmp_dir / "worker.git")]
        repos_dir = tmp_dir / "repos"
        git = make_git(tmp_dir)
        await sync_code_repos(repos=repos, repos_dir=repos_dir, git=git)
        new_head = await commit_files(work, {"src/B.kt": "class B"}, "B")
        await work.run(["push", "-q", "origin", "main"])
        result = await sync_code_repos(repos=repos, repos_dir=repos_dir, git=git)
        assert result.heads["worker"] == new_head
        assert await rev_parse(make_git(repos_dir / "worker"), "HEAD") == new_head
        assert (repos_dir / "worker" / "src" / "B.kt").read_text() == "class B"

    async def test_커밋된_심볼릭_링크도_일반_파일로_받아진다(self, tmp_dir: Path) -> None:
        work = await seed_bare(tmp_dir / "worker.git", tmp_dir / "w", {"src/A.kt": "class A"})
        os.symlink("../outside-target", work.cwd / "link")
        await work.run(["add", "-A"])
        await work.run(["commit", "-q", "-m", "link"])
        await work.run(["push", "-q", "origin", "main"])
        result = await sync_code_repos(
            repos=[active("worker", tmp_dir / "worker.git")],
            repos_dir=tmp_dir / "repos",
            git=make_git(tmp_dir),
        )
        assert result.failures == {}
        link = tmp_dir / "repos" / "worker" / "link"
        assert not link.is_symlink()
        assert link.is_file()

    async def test_예전_clone에_이미_풀린_링크도_일반_파일이_된다(self, tmp_dir: Path) -> None:
        work = await seed_bare(tmp_dir / "worker.git", tmp_dir / "w", {"src/A.kt": "class A"})
        os.symlink("A.kt", work.cwd / "src" / "link")
        await work.run(["add", "-A"])
        await work.run(["commit", "-q", "-m", "link"])
        await work.run(["push", "-q", "origin", "main"])
        repos_dir = tmp_dir / "repos"
        # 예전 방식(core.symlinks=true)으로 받은 clone을 만든다.
        await make_git(tmp_dir).run(
            ["clone", "-q", "-c", "core.symlinks=true", "--", str(tmp_dir / "worker.git"),
             str(repos_dir / "worker")]
        )  # fmt: skip
        assert (repos_dir / "worker" / "src" / "link").is_symlink()
        result = await sync_code_repos(
            repos=[active("worker", tmp_dir / "worker.git")],
            repos_dir=repos_dir,
            git=make_git(tmp_dir),
        )
        assert result.failures == {}
        assert not (repos_dir / "worker" / "src" / "link").is_symlink()

    async def test_원격_URL에_비밀번호가_있어도_실패_메시지에_싣지_않는다(
        self, tmp_dir: Path
    ) -> None:
        url = "https://user:hunter2secret@example.invalid/org/worker.git"
        result = await sync_code_repos(
            repos=[active("worker", url)],
            repos_dir=tmp_dir / "repos",
            git=make_git(tmp_dir),
        )
        assert result.heads == {}
        assert list(result.failures) == ["worker"]
        assert "hunter2secret" not in result.failures["worker"]
        assert "user:" not in result.failures["worker"]

    async def test_기존_clone_원격에_비밀번호가_있으면_설정_URL로_바꿔_복구한다(
        self, tmp_dir: Path
    ) -> None:
        await seed_bare(tmp_dir / "worker.git", tmp_dir / "w", {"src/A.kt": "class A"})
        repos_dir = tmp_dir / "repos"
        git = make_git(tmp_dir)
        repos = [active("worker", tmp_dir / "worker.git")]
        await sync_code_repos(repos=repos, repos_dir=repos_dir, git=git)
        clone = make_git(repos_dir / "worker")
        await clone.run(
            ["config", "remote.origin.url", "https://user:hunter2secret@example.invalid/x.git"]
        )
        result = await sync_code_repos(repos=repos, repos_dir=repos_dir, git=git)
        assert result.failures == {}
        assert await remote_url(clone) == str(tmp_dir / "worker.git")

    async def test_설정의_저장소_URL이_바뀌면_기존_코드_사본의_원격이_따라_바뀐다(
        self, tmp_dir: Path
    ) -> None:
        await seed_bare(tmp_dir / "a.git", tmp_dir / "w", {"src/A.kt": "class A"})
        await seed_bare(tmp_dir / "b.git", tmp_dir / "w2", {"src/A.kt": "class A"}, "other")
        repos_dir = tmp_dir / "repos"
        git = make_git(tmp_dir)
        await sync_code_repos(
            repos=[active("worker", tmp_dir / "a.git")], repos_dir=repos_dir, git=git
        )
        clone = make_git(repos_dir / "worker")
        assert await remote_url(clone) == str(tmp_dir / "a.git")
        result = await sync_code_repos(
            repos=[active("worker", tmp_dir / "b.git")], repos_dir=repos_dir, git=git
        )
        assert result.failures == {}
        assert await remote_url(clone) == str(tmp_dir / "b.git")
        assert result.heads["worker"] == await rev_parse(make_git(tmp_dir / "w2"), "HEAD")

    async def test_받기_실패가_난_저장소가_있어도_다음_저장소를_받는다(self, tmp_dir: Path) -> None:
        await seed_bare(tmp_dir / "worker.git", tmp_dir / "w", {"src/A.kt": "class A"})
        result = await sync_code_repos(
            repos=[
                active("a-bad", tmp_dir / "nope.git"),
                active("worker", tmp_dir / "worker.git"),
            ],
            repos_dir=tmp_dir / "repos",
            git=make_git(tmp_dir),
        )
        assert list(result.failures) == ["a-bad"]
        assert list(result.heads) == ["worker"]

    async def test_없는_브랜치는_실패로_모으고_폴더_밖을_건드리지_않는다(
        self, tmp_dir: Path
    ) -> None:
        await seed_bare(tmp_dir / "worker.git", tmp_dir / "w", {"src/A.kt": "class A"})
        result = await sync_code_repos(
            repos=[active("worker", tmp_dir / "worker.git", branch="nobranch")],
            repos_dir=tmp_dir / "repos",
            git=make_git(tmp_dir),
        )
        assert result.heads == {}
        assert list(result.failures) == ["worker"]

    async def test_저장소_폴더가_바깥_clone을_가리키는_링크면_실패로_모으고_바깥을_건드리지_않는다(
        self, tmp_dir: Path
    ) -> None:
        await seed_bare(tmp_dir / "worker.git", tmp_dir / "w", {"src/A.kt": "class A"})
        victim = await seed_bare(tmp_dir / "v.git", tmp_dir / "victim", {"src/A.kt": "class A"})
        (victim.cwd / "src" / "A.kt").write_text("편집 중", encoding="utf-8")
        (victim.cwd / "precious.txt").write_text("지우면 안 됨", encoding="utf-8")
        repos_dir = tmp_dir / "repos"
        repos_dir.mkdir()
        os.symlink(victim.cwd, repos_dir / "worker")
        result = await sync_code_repos(
            repos=[
                active("worker", tmp_dir / "worker.git"),
                active("other", tmp_dir / "worker.git"),
            ],
            repos_dir=repos_dir,
            git=make_git(tmp_dir),
        )
        assert list(result.failures) == ["worker"]
        assert list(result.heads) == ["other"]
        assert (victim.cwd / "precious.txt").read_text(encoding="utf-8") == "지우면 안 됨"
        assert (victim.cwd / "src" / "A.kt").read_text(encoding="utf-8") == "편집 중"
        assert await remote_url(make_git(victim.cwd)) == str(tmp_dir / "v.git")

    async def test_점유된_폴더는_실패로_모은다(self, tmp_dir: Path) -> None:
        await seed_bare(tmp_dir / "worker.git", tmp_dir / "w", {"src/A.kt": "class A"})
        (tmp_dir / "repos" / "worker").mkdir(parents=True)
        (tmp_dir / "repos" / "worker" / "stray.txt").write_text("x")
        result = await sync_code_repos(
            repos=[active("worker", tmp_dir / "worker.git")],
            repos_dir=tmp_dir / "repos",
            git=make_git(tmp_dir),
        )
        assert result.heads == {}
        assert "worker" in result.failures
        assert len(result.failures["worker"]) <= 300 + 1


NOW1 = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
NOW2 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class TestCodeSnapshot:
    def test_코드_사본_시점을_기록하고_이번에_못_받은_저장소는_이전_값을_둔다(
        self, tmp_dir: Path
    ) -> None:
        write_code_snapshot(tmp_dir, {"worker": "a" * 40, "web": "b" * 40}, NOW1)
        write_code_snapshot(tmp_dir, {"worker": "c" * 40}, NOW2)
        snapshot = read_code_snapshot(tmp_dir)
        assert snapshot == CodeSnapshot(
            fetched_at="2026-10-07T21:00:00+09:00",
            repos={"worker": "c" * 40, "web": "b" * 40},
        )
        assert read_code_snapshot(tmp_dir / "empty") is None

    def test_파일_이름과_키는_snake_case다(self, tmp_dir: Path) -> None:
        write_code_snapshot(tmp_dir, {"worker": "a" * 40}, NOW1)
        text = (tmp_dir / STATE_FILES.code_snapshot).read_text(encoding="utf-8")
        assert '"fetched_at"' in text

    def test_쓰레기_파일이면_None이다(self, tmp_dir: Path) -> None:
        file = tmp_dir / STATE_FILES.code_snapshot
        for garbage in ("쓰레기", "[]", '{"fetched_at": 1, "repos": {}}', '{"repos": {}}', ""):
            file.write_text(garbage, encoding="utf-8")
            assert read_code_snapshot(tmp_dir) is None

    def test_쓰레기_파일_위에_써도_새_값만_남는다(self, tmp_dir: Path) -> None:
        (tmp_dir / STATE_FILES.code_snapshot).write_text("쓰레기", encoding="utf-8")
        write_code_snapshot(tmp_dir, {"worker": "a" * 40}, NOW1)
        assert read_code_snapshot(tmp_dir) == CodeSnapshot(
            fetched_at="2026-10-07T09:00:00+09:00", repos={"worker": "a" * 40}
        )

    def test_모델은_불변이다(self) -> None:
        snapshot = CodeSnapshot(fetched_at="x", repos={})
        try:
            snapshot.fetched_at = "y"
        except ValidationError:
            return
        raise AssertionError("frozen이어야 합니다")


class TestBasisLines:
    def test_기록이_없으면_없음으로_채운다(self) -> None:
        assert basis_lines(None, None) == [
            "[위키 기준]",
            "반영 시각: 기록 없음",
            "저장소 기준: 없음",
            "",
            "[코드 사본 시점]",
            "없음 (기록 없음)",
        ]

    def test_값이_있으면_KST_라벨과_짧은_SHA로_적는다(self) -> None:
        baseline = Baseline(
            repos={"worker": RepoBaseline("a" * 40, False)},
            updated_at="2026-10-07T00:00:00Z",
        )
        snapshot = CodeSnapshot(fetched_at="2026-10-07T00:30:00Z", repos={"worker": "b" * 40})
        assert basis_lines(baseline, snapshot) == [
            "[위키 기준]",
            "반영 시각: 2026-10-07 09:00",
            "저장소 기준: worker aaaaaaa",
            "",
            "[코드 사본 시점]",
            "worker bbbbbbb (2026-10-07 09:30)",
        ]

    def test_이름순으로_가운뎃점으로_잇는다(self) -> None:
        baseline = Baseline(
            repos={"web": RepoBaseline("2" * 40, False), "api": RepoBaseline("1" * 40, True)},
            updated_at=None,
        )
        lines = basis_lines(baseline, None)
        assert lines[1] == "반영 시각: 기록 없음"
        assert lines[2] == "저장소 기준: api 1111111 · web 2222222"

    def test_읽을_수_없는_시각은_기록_없음이다(self) -> None:
        baseline = Baseline(updated_at="쓰레기")
        snapshot = CodeSnapshot(fetched_at="쓰레기", repos={"worker": "b" * 40})
        lines = basis_lines(baseline, snapshot)
        assert lines[1] == "반영 시각: 기록 없음"
        assert lines[5] == "worker bbbbbbb (기록 없음)"

    def test_이름과_SHA의_제어_문자는_이스케이프한다(self) -> None:
        baseline = Baseline(repos={"a\nb‮": RepoBaseline("c" * 40, False)})
        snapshot = CodeSnapshot(fetched_at="x", repos={"x\x1b[2J": "d\n\ne‮fghi"})
        lines = basis_lines(baseline, snapshot)
        assert len(lines) == 6
        assert all("\n" not in line and "‮" not in line and "\x1b" not in line for line in lines)
        assert "a\\nb\\u202e ccccccc" in lines[2]

    def test_외톨이_서로게이트가_있어도_인코딩할_수_있다(self) -> None:
        snapshot = CodeSnapshot(fetched_at="x", repos={"w\udc80": "\udc80" * 10})
        for line in basis_lines(None, snapshot):
            line.encode("utf-8")

    def test_저장소가_아주_많아도_목록은_상한을_넘지_않는다(self) -> None:
        snapshot = CodeSnapshot(
            fetched_at="2026-10-07T00:00:00Z",
            repos={f"r{i:05d}": "a" * 40 for i in range(5000)},
        )
        line = basis_lines(None, snapshot)[5]
        assert len(line) < 5000
        assert "r00000 aaaaaaa" in line
        assert "외 " in line

    def test_이름이_아주_길어도_잘라서_적는다(self) -> None:
        snapshot = CodeSnapshot(fetched_at="x", repos={"n" * 5000: "a" * 40})
        assert len(basis_lines(None, snapshot)[5]) < 400


# ---------------------------------------------------------------- 취소·남은 잠금·끊긴 clone

MANY_FILES = {f"src/F{i:03}.kt": "class F" + str(i) + "\n" * 50 for i in range(300)}


async def _cancelled_sync(
    repos: list[RepoConfig], repos_dir: Path, git: GitClient, delay: float
) -> None:
    task = asyncio.ensure_future(sync_code_repos(repos=repos, repos_dir=repos_dir, git=git))
    await asyncio.sleep(delay)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestCodeCopyRecovery:
    @pytest.mark.parametrize("warm", [False, True], ids=["처음_받기", "기존_사본"])
    async def test_취소를_어느_시점에_해도_다음_동기화가_성공하고_새_커밋을_반영한다(
        self, tmp_dir: Path, warm: bool
    ) -> None:
        bare = tmp_dir / "worker.git"
        work = await seed_bare(bare, tmp_dir / "w", MANY_FILES)
        repos = [active("worker", bare)]
        repos_dir = tmp_dir / "repos"
        git = make_git(tmp_dir)
        if warm:
            assert (await sync_code_repos(repos=repos, repos_dir=repos_dir, git=git)).failures == {}
        trials = 30
        for k in range(trials):
            if not warm:
                shutil.rmtree(repos_dir, ignore_errors=True)
            head = await commit_files(work, {f"new{k}.txt": str(k)}, f"c{k}")
            await work.run(["push", "-q", "origin", "main"])
            await _cancelled_sync(repos, repos_dir, git, delay=0.15 * k / trials)
            result = await sync_code_repos(repos=repos, repos_dir=repos_dir, git=git)
            assert result.failures == {}, (k, result.failures)
            assert result.heads == {"worker": head}, k

    async def test_남은_잠금_파일은_지우고_다시_시도한다(self, tmp_dir: Path) -> None:
        bare = tmp_dir / "worker.git"
        work = await seed_bare(bare, tmp_dir / "w", {"src/A.kt": "class A"})
        repos = [active("worker", bare)]
        git = make_git(tmp_dir)
        await sync_code_repos(repos=repos, repos_dir=tmp_dir / "repos", git=git)
        head = await commit_files(work, {"src/B.kt": "class B"}, "B")
        await work.run(["push", "-q", "origin", "main"])
        dot_git = tmp_dir / "repos" / "worker" / ".git"
        for name in ("index.lock", "HEAD.lock", "refs/remotes/origin/main.lock"):
            (dot_git / name).parent.mkdir(parents=True, exist_ok=True)
            (dot_git / name).write_text("", encoding="utf-8")
        result = await sync_code_repos(repos=repos, repos_dir=tmp_dir / "repos", git=git)
        assert result.failures == {}
        assert result.heads == {"worker": head}
        assert not (dot_git / "index.lock").exists()
        assert not (dot_git / "HEAD.lock").exists()

    async def test_링크인_잠금은_지우지_않고_실패로_모은다(self, tmp_dir: Path) -> None:
        bare = tmp_dir / "worker.git"
        await seed_bare(bare, tmp_dir / "w", {"src/A.kt": "class A"})
        repos = [active("worker", bare)]
        git = make_git(tmp_dir)
        await sync_code_repos(repos=repos, repos_dir=tmp_dir / "repos", git=git)
        target = tmp_dir / "target.txt"
        target.write_text("그대로", encoding="utf-8")
        os.symlink(target, tmp_dir / "repos" / "worker" / ".git" / "index.lock")
        result = await sync_code_repos(repos=repos, repos_dir=tmp_dir / "repos", git=git)
        assert list(result.failures) == ["worker"]
        assert target.read_text(encoding="utf-8") == "그대로"

    async def test_HEAD가_없는_반쪽_clone은_다시_받는다(self, tmp_dir: Path) -> None:
        bare = tmp_dir / "worker.git"
        work = await seed_bare(bare, tmp_dir / "w", {"src/A.kt": "class A"})
        head = await rev_parse(work, "HEAD")
        half = tmp_dir / "repos" / "worker"
        (half / ".git").mkdir(parents=True)
        (half / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        result = await sync_code_repos(
            repos=[active("worker", bare)], repos_dir=tmp_dir / "repos", git=make_git(tmp_dir)
        )
        assert result.failures == {}
        assert result.heads == {"worker": head}

    async def test_git이_링크면_다시_받지_않고_실패로_모은다(self, tmp_dir: Path) -> None:
        bare = tmp_dir / "worker.git"
        await seed_bare(bare, tmp_dir / "w", {"src/A.kt": "class A"})
        other = tmp_dir / "other-git"
        other.mkdir()
        (tmp_dir / "repos" / "worker").mkdir(parents=True)
        os.symlink(other, tmp_dir / "repos" / "worker" / ".git")
        result = await sync_code_repos(
            repos=[active("worker", bare)], repos_dir=tmp_dir / "repos", git=make_git(tmp_dir)
        )
        assert list(result.failures) == ["worker"]
        assert list(other.iterdir()) == []

    async def test_취소돼도_조각_폴더를_남기지_않는다(self, tmp_dir: Path) -> None:
        bare = tmp_dir / "worker.git"
        await seed_bare(bare, tmp_dir / "w", MANY_FILES)
        repos_dir = tmp_dir / "repos"
        await _cancelled_sync([active("worker", bare)], repos_dir, make_git(tmp_dir), delay=0.0)
        leftovers = [p.name for p in repos_dir.iterdir()] if repos_dir.exists() else []
        assert all(not n.startswith(".worker.partial-") for n in leftovers)


# ---------------------------------------------------------------- 보호 경계(변이로 확인한 것)

PATCH = "team_wiki_engine.sync.repos."


async def _warm(tmp_dir: Path) -> tuple[GitClient, list[RepoConfig], GitClient]:
    bare = tmp_dir / "worker.git"
    work = await seed_bare(bare, tmp_dir / "w", {"src/A.kt": "class A"})
    repos = [active("worker", bare)]
    git = make_git(tmp_dir)
    assert (await sync_code_repos(repos=repos, repos_dir=tmp_dir / "repos", git=git)).failures == {}
    return work, repos, git


class TestProtectionBoundaries:
    async def test_실패_항목에_로컬_절대_경로가_없다(self, tmp_dir: Path) -> None:
        result = await sync_code_repos(
            repos=[active("gone", tmp_dir / "nope.git")],
            repos_dir=tmp_dir / "repos",
            git=make_git(tmp_dir),
        )
        assert list(result.failures) == ["gone"]
        assert str(tmp_dir) not in result.failures["gone"]
        assert "/private/" not in result.failures["gone"]

    async def test_fetch는_취소를_바로_전달한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare = tmp_dir / "worker.git"
        await seed_bare(bare, tmp_dir / "w", {"src/A.kt": "class A"})
        started = asyncio.Event()

        async def slow_fetch(_git: GitClient, _branch: str) -> None:
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(PATCH + "fetch_branch", slow_fetch)
        task = asyncio.ensure_future(
            sync_code_repos(
                repos=[active("worker", bare)], repos_dir=tmp_dir / "repos", git=make_git(tmp_dir)
            )
        )
        await started.wait()
        begin = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(10):
                await task
        assert time.monotonic() - begin < 2  # 최대 600초 걸리는 fetch를 기다리지 않는다

    async def test_clone은_조각_폴더에서_끝난_뒤에만_제자리에_놓인다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare = tmp_dir / "worker.git"
        await seed_bare(bare, tmp_dir / "w", {"src/A.kt": "class A"})
        repos_dir = tmp_dir / "repos"
        cloned = asyncio.Event()
        visible: list[bool] = []

        async def clone_then_stall(git: GitClient, url: str) -> None:
            await real_ensure_clone(git, url)
            visible.append((repos_dir / "worker").exists())  # 받은 직후, 놓기 전
            cloned.set()
            await asyncio.sleep(30)

        monkeypatch.setattr("team_wiki_engine.sync.data_copy.ensure_clone", clone_then_stall)
        task = asyncio.ensure_future(
            sync_code_repos(
                repos=[active("worker", bare)], repos_dir=repos_dir, git=make_git(tmp_dir)
            )
        )
        await cloned.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert visible == [False]
        assert not (repos_dir / "worker").exists()
        assert [p.name for p in repos_dir.iterdir()] == []  # 조각도 남지 않았다

    async def test_죽은_프로세스가_남긴_조각_폴더는_시작할_때_치운다(self, tmp_dir: Path) -> None:
        work, repos, git = await _warm(tmp_dir)
        del work
        stale = tmp_dir / "repos" / ".worker.partial-2147483000-deadbeef"
        stale.mkdir()
        (stale / "x").write_text("x", encoding="utf-8")
        result = await sync_code_repos(repos=repos, repos_dir=tmp_dir / "repos", git=git)
        assert result.failures == {}
        assert not stale.exists()

    async def test_checkout은_취소돼도_끝까지_간다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work, repos, git = await _warm(tmp_dir)
        head = await commit_files(work, {"src/B.kt": "class B"}, "B")
        await work.run(["push", "-q", "origin", "main"])
        holder: list[asyncio.Future[CodeSyncResult]] = []

        async def cancelling(g: GitClient, ref: str) -> None:
            holder[0].cancel()
            await real_checkout_detached(g, ref)

        monkeypatch.setattr(PATCH + "checkout_detached", cancelling)
        task = asyncio.ensure_future(
            sync_code_repos(repos=repos, repos_dir=tmp_dir / "repos", git=git)
        )
        holder.append(task)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await rev_parse(make_git(tmp_dir / "repos" / "worker"), "HEAD") == head

    async def test_원격_주소_바로잡기는_취소돼도_끝까지_간다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _work, repos, git = await _warm(tmp_dir)
        clone = make_git(tmp_dir / "repos" / "worker")
        await clone.run(["remote", "set-url", "origin", str(tmp_dir / "elsewhere.git")])
        holder: list[asyncio.Future[CodeSyncResult]] = []

        async def cancelling(g: GitClient, url: str) -> None:
            holder[0].cancel()
            await real_set_remote_url(g, url)

        monkeypatch.setattr(PATCH + "set_remote_url", cancelling)
        task = asyncio.ensure_future(
            sync_code_repos(repos=repos, repos_dir=tmp_dir / "repos", git=git)
        )
        holder.append(task)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await remote_url(clone) == repos[0].url

    async def test_링크_지우기는_취소돼도_끝까지_간다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _work, repos, git = await _warm(tmp_dir)
        link = tmp_dir / "repos" / "worker" / "stray-link"
        os.symlink(tmp_dir, link)
        holder: list[asyncio.Future[CodeSyncResult]] = []

        async def cancelling(g: GitClient) -> None:
            holder[0].cancel()
            await real_disable_symlinks(g)

        monkeypatch.setattr(PATCH + "disable_symlinks", cancelling)
        task = asyncio.ensure_future(
            sync_code_repos(repos=repos, repos_dir=tmp_dir / "repos", git=git)
        )
        holder.append(task)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not os.path.lexists(link)

    async def test_다시_시도도_실패하면_처음_오류를_돌려준다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare = tmp_dir / "worker.git"
        await seed_bare(bare, tmp_dir / "w", {"src/A.kt": "class A"})
        calls: list[int] = []

        async def failing(_clone: GitClient, _repo: RepoConfig) -> str:
            calls.append(1)
            raise GitError("오류 " + ("첫째" if len(calls) == 1 else "둘째"), kind="exit", code=1)

        monkeypatch.setattr(PATCH + "_update", failing)
        monkeypatch.setattr(PATCH + "remove_stale_git_locks", lambda _g, _b: 1)
        result = await sync_code_repos(
            repos=[active("worker", bare)], repos_dir=tmp_dir / "repos", git=make_git(tmp_dir)
        )
        assert len(calls) == 2
        assert "첫째" in result.failures["worker"]
        assert "둘째" not in result.failures["worker"]

    async def test_남은_maintenance_lock도_회복_목록에_있다(self, tmp_dir: Path) -> None:
        _work, _repos, _git = await _warm(tmp_dir)
        clone = make_git(tmp_dir / "repos" / "worker")
        lock = clone.cwd / ".git" / "objects" / "maintenance.lock"
        lock.write_text("", encoding="utf-8")
        assert remove_stale_git_locks(clone, "main") == 1
