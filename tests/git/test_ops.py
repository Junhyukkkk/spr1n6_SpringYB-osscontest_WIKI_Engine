"""git 작업(실제 git + 임시 저장소). 입력 검증은 가짜 git의 호출 기록으로 확인한다."""

import asyncio
import inspect
import os
import shutil
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.git import ops
from team_wiki_engine.git.client import GitClient, GitError, GitOutputTooLarge, GitResult
from team_wiki_engine.git.ops import (
    EMPTY_TREE,
    CloneStateError,
    DiffText,
    NumStat,
    author_names,
    changed_paths,
    checkout_detached,
    commit_all,
    commit_date,
    commit_paths,
    diff_names,
    diff_text,
    disable_symlinks,
    discard_changes,
    ensure_clone,
    fetch_branch,
    first_parent_log,
    is_ancestor,
    numstat,
    push,
    remote_url,
    reset_branch_to,
    rev_parse,
    set_remote_url,
    try_rev_parse,
)
from tests.helpers import make_tmp_dir, write_tree
from tests.helpers.fake_git import fake_git_calls, write_fake_git
from tests.helpers.git import commit_files, init_repo, seed_bare

LINK_TARGET = "../../etc/passwd"
SINCE = datetime(2000, 1, 1, tzinfo=UTC)


def _client(directory: Path) -> GitClient:
    return GitClient(directory, allowed_protocols="file", timeout_s=60.0)


def _mark_repo(directory: Path) -> None:
    """가짜 git 시험용으로 보호 장치를 통과하는 `.git`(폴더 + 정상 모양의 `HEAD`)을 만든다."""
    (directory / ".git").mkdir(parents=True, exist_ok=True)
    (directory / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def _fake_client(
    root: Path, cwd: Path | None = None, *, repo: bool = False
) -> tuple[GitClient, Path]:
    """`root/bin/git`(가짜)을 쓰는 클라이언트. `cwd`는 기본이 `root`(아직 없어도 된다)."""
    script = write_fake_git(root / "bin")
    if repo:
        _mark_repo(cwd or root)
    return GitClient(cwd or root, git_bin=str(script), allowed_protocols="file"), script


def _sorted(paths: Sequence[str]) -> list[str]:
    return sorted(paths)


async def _ref(git: GitClient, ref: str) -> str:
    return (await git.run(["rev-parse", ref])).text.strip()


# ---------------------------------------------------------------- 공유 저장소(읽기 전용)


@dataclass(frozen=True)
class World:
    bare: Path
    main: str


@dataclass(frozen=True)
class History:
    git: GitClient
    base: str
    merge: str
    direct: str
    head: str


@pytest.fixture(scope="module")
def world() -> Iterator[World]:
    """`wiki/link.md -> ../../etc/passwd` 링크가 든 bare 저장소(읽기·새 브랜치 push만 한다)."""
    root = make_tmp_dir("twe-world-")

    async def build() -> World:
        work = await seed_bare(
            root / "r.git", root / "w", {"wiki/index.md": "# 색인\n", ".gitignore": ".DS_Store\n"}
        )
        os.symlink(LINK_TARGET, root / "w" / "wiki" / "link.md")
        main = await commit_files(work, {}, "링크 추가")
        await work.run(["push", "-q", "origin", "main"])
        return World(root / "r.git", main)

    try:
        yield asyncio.run(build())
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def history() -> Iterator[History]:
    """머지·직접 커밋·한글 경로가 든 저장소. diff 외부 도구·textconv가 깨져 있어 플래그가 필수다."""
    root = make_tmp_dir("twe-history-")

    async def build() -> History:
        git = await init_repo(
            root / "h",
            {"a.txt": "a\n", "wiki/a.md": "a\n", ".gitattributes": "*.md diff=bad\n"},
        )
        base = await _ref(git, "HEAD")
        await git.run(["checkout", "-q", "-b", "feat"])
        await commit_files(git, {"f.txt": "f\n"}, "feature work")
        await git.run(["checkout", "-q", "main"])
        await git.run(["merge", "-q", "--no-ff", "-m", "Merge pull request #3 from feat", "feat"])
        merge = await _ref(git, "HEAD")
        direct = await commit_files(git, {"d.txt": "d\n", "wiki/b.md": "b\nc\n"}, "직접 커밋")
        head = await commit_files(
            git,
            {
                "wiki/a.md": "a\nb\nc\n",
                "wiki/한글 문서.md": "한글\n",
                "한글/파일.md": "x" * 5000 + "\n",
            },
            "wiki 수정",
        )
        with (root / "h" / ".git" / "config").open("a", encoding="utf-8") as config:
            config.write(
                '[diff]\n\texternal = /nonexistent-ext-diff\n[diff "bad"]\n'
                "\ttextconv = /nonexistent-textconv\n"
            )
        return History(git, base, merge, direct, head)

    try:
        yield asyncio.run(build())
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 입력 검증(G12)

BAD_REFS: list[Any] = [
    "--output=/tmp/x",
    "-h",
    "--upload-pack=touch /tmp/x",
    "a..b",
    "x\ny",
    "",
    "HEAD~1",
    "HEAD^",
    "main^{commit}",
    "origin/",
    "origin/../x",
    "a" * 40 + "\n",
    "refs/heads/a:b",
    "+main",
    None,
    123,
    b"main",
]

RefCall = Callable[[GitClient, Any], Awaitable[object]]
ISO = datetime(2026, 10, 7, tzinfo=UTC)
REF_CALLS: dict[str, RefCall] = {
    "rev_parse": lambda g, r: rev_parse(g, r),
    "try_rev_parse": lambda g, r: try_rev_parse(g, r),
    "checkout_detached": lambda g, r: checkout_detached(g, r),
    "reset_branch_to.branch": lambda g, r: reset_branch_to(g, r, "HEAD"),
    "reset_branch_to.ref": lambda g, r: reset_branch_to(g, "main", r),
    "fetch_branch": lambda g, r: fetch_branch(g, r),
    "first_parent_log.from": lambda g, r: first_parent_log(g, r, "HEAD"),
    "first_parent_log.to": lambda g, r: first_parent_log(g, None, r),
    "commit_date": lambda g, r: commit_date(g, r),
    "author_names": lambda g, r: author_names(g, r, ISO),
    "push.local": lambda g, r: push(g, r, "main"),
    "push.remote": lambda g, r: push(g, "HEAD", r),
    "is_ancestor.a": lambda g, r: is_ancestor(g, r, "HEAD"),
    "is_ancestor.d": lambda g, r: is_ancestor(g, "HEAD", r),
    "diff_names.from": lambda g, r: diff_names(g, r, "HEAD", ["wiki"]),
    "diff_names.to": lambda g, r: diff_names(g, "HEAD", r, ["wiki"]),
    "numstat.from": lambda g, r: numstat(g, r, "HEAD", "wiki"),
    "numstat.to": lambda g, r: numstat(g, "HEAD", r, "wiki"),
    "diff_text.from": lambda g, r: diff_text(g, r, "HEAD", 1000),
    "diff_text.to": lambda g, r: diff_text(g, "HEAD", r, 1000),
}

NONE_ALLOWED = {"first_parent_log.from", "diff_text.from"}  # `None` = 처음 읽는 저장소


@pytest.mark.parametrize("name", sorted(REF_CALLS))
async def test_invalid_refs_raise_value_error_without_calling_git(name: str, tmp_dir: Path) -> None:
    git, script = _fake_client(tmp_dir)
    for bad in BAD_REFS:
        if bad is None and name in NONE_ALLOWED:
            continue
        with pytest.raises(ValueError):
            await REF_CALLS[name](git, bad)
    assert fake_git_calls(script) == []


async def test_valid_refs_do_reach_git(tmp_dir: Path) -> None:
    """위 시험이 헛돌지 않는다는 대조군: 올바른 인자는 git을 부른다."""
    git, script = _fake_client(tmp_dir, repo=True)
    for ref in ["HEAD", "main", "origin/main", "sync/2026-10-07", "a" * 40, EMPTY_TREE]:
        assert await try_rev_parse(git, ref) is None  # 가짜 git은 종료 코드 1
    first = fake_git_calls(script)[0]
    assert first[0].startswith("--git-dir=") and first[1].startswith("--work-tree=")
    assert first[2:] == ["rev-parse", "--verify", "-q", "HEAD^{commit}"]
    assert len(fake_git_calls(script)) == 6


async def test_other_invalid_inputs_do_not_call_git(tmp_dir: Path) -> None:
    git, script = _fake_client(tmp_dir)
    with pytest.raises(ValueError):
        await commit_all(git, "x\0y")
    with pytest.raises(ValueError):
        await commit_all(git, "")
    with pytest.raises(ValueError):
        await diff_names(git, "HEAD", "HEAD", ["ok", "bad\0path"])
    with pytest.raises(ValueError):
        await diff_names(git, "HEAD", "HEAD", [""])
    with pytest.raises(ValueError):
        await numstat(git, "HEAD", "HEAD", "bad\0")
    with pytest.raises(ValueError):
        await diff_text(git, None, "HEAD", -1)
    with pytest.raises(ValueError):
        await author_names(git, "HEAD", "2026-10-07")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await set_remote_url(git, "--upload-pack=x")
    with pytest.raises(ValueError):
        await commit_all(git, "a" * 65_537)  # 65,536바이트 상한(UTF-8)
    with pytest.raises(ValueError):
        await commit_all(git, "한" * 21_846)  # 65,538바이트
    with pytest.raises(ValueError):
        await commit_all(git, "lone surrogate \ud800")
    assert fake_git_calls(script) == []


@pytest.mark.parametrize(
    "url",
    [
        "",
        "-x",
        "--upload-pack=touch /tmp/x",
        "ext::sh -c touch% /tmp/x",
        "EXT::sh -c x",
        "fd::7",
        "helper::anything",
        "https://example.com/r.git\n",
        "https://example.com/\x00r.git",
        "https://example.com/\x1br.git",
        "https://example.com/\x7fr.git",
        "https://example.com/\x85r.git",
        "https://example.com/" + "a" * 2048,
        "   ",
        "\t",
        "https://user:ghp_tok@github.com/o/n.git",
        "https://ghp_tok@github.com/o/n.git",
        "https://:@github.com/o/n.git",
        "https://example.com/\ud800",
        None,
        123,
    ],
)
async def test_ensure_clone_rejects_bad_urls_before_anything(url: Any, tmp_dir: Path) -> None:
    git, script = _fake_client(tmp_dir, tmp_dir / "parent" / "clone")
    with pytest.raises(ValueError):
        await ensure_clone(git, url)
    assert fake_git_calls(script) == []
    assert not (tmp_dir / "parent").exists()


async def test_url_error_message_does_not_echo_credentials(tmp_dir: Path) -> None:
    git, _ = _fake_client(tmp_dir, tmp_dir / "c")
    with pytest.raises(ValueError) as caught:
        await ensure_clone(git, "https://user:ghp_secret_token_1234567@host/r.git\n")
    assert "ghp_secret" not in str(caught.value)


async def test_upload_pack_url_creates_no_file(tmp_dir: Path) -> None:
    marker = tmp_dir / "x"
    git = _client(tmp_dir / "p" / "clone")
    with pytest.raises(ValueError):
        await ensure_clone(git, f"--upload-pack=touch {marker}")
    assert not marker.exists()


def test_is_safe_branch_and_is_sha_reject_trailing_newline() -> None:
    assert not ops.is_safe_branch("main\n")
    assert not ops.is_sha("a" * 40 + "\n")


# ---------------------------------------------------------------- ensure_clone


async def test_ensure_clone_argv_and_parent_dir(tmp_dir: Path) -> None:
    dest = tmp_dir / "new" / "deep" / "clone"
    git, script = _fake_client(tmp_dir, dest)
    url = "https://example.com/team/repo.git"
    with pytest.raises(GitError):  # 가짜 git은 clone을 모른다
        await ensure_clone(git, url)
    assert fake_git_calls(script) == [
        ["clone", "-q", "--no-tags", "-c", "core.symlinks=false", "--", url, str(dest)]
    ]
    assert dest.parent.is_dir()


async def test_ensure_clone_existing_real_git_dir_does_nothing(tmp_dir: Path) -> None:
    (tmp_dir / "c" / ".git").mkdir(parents=True)
    git, script = _fake_client(tmp_dir, tmp_dir / "c")
    await ensure_clone(git, "https://example.com/r.git")
    assert fake_git_calls(script) == []


async def test_ensure_clone_refuses_odd_git_entries(tmp_dir: Path) -> None:
    git, script = _fake_client(tmp_dir, tmp_dir / "link")
    (tmp_dir / "link").mkdir()
    (tmp_dir / "elsewhere" / ".git").mkdir(parents=True)
    (tmp_dir / "link" / ".git").symlink_to(tmp_dir / "elsewhere" / ".git")
    with pytest.raises(CloneStateError):
        await ensure_clone(git, "https://example.com/r.git")

    git, _ = _fake_client(tmp_dir, tmp_dir / "file")
    (tmp_dir / "file").mkdir()
    (tmp_dir / "file" / ".git").write_text("gitdir: ../elsewhere/.git\n", encoding="utf-8")
    with pytest.raises(CloneStateError):
        await ensure_clone(git, "https://example.com/r.git")

    git, _ = _fake_client(tmp_dir, tmp_dir / "dangling")
    (tmp_dir / "dangling").mkdir()
    (tmp_dir / "dangling" / ".git").symlink_to(tmp_dir / "missing")
    with pytest.raises(CloneStateError):
        await ensure_clone(git, "https://example.com/r.git")

    git, _ = _fake_client(tmp_dir, tmp_dir / "full")
    write_tree(tmp_dir / "full", {"keep.txt": "k"})
    with pytest.raises(CloneStateError):
        await ensure_clone(git, "https://example.com/r.git")
    assert (tmp_dir / "full" / "keep.txt").read_text(encoding="utf-8") == "k"

    git, _ = _fake_client(tmp_dir, tmp_dir / "plainfile")
    (tmp_dir / "plainfile").write_text("x", encoding="utf-8")
    with pytest.raises(CloneStateError):
        await ensure_clone(git, "https://example.com/r.git")
    assert fake_git_calls(script) == []


async def test_ensure_clone_flattens_committed_symlink(world: World, tmp_dir: Path) -> None:
    """G4: 커밋된 링크가 링크 대상 글을 담은 일반 파일로 나오고 저장소 설정이 남는다."""
    clone = tmp_dir / "a" / "clone"
    git = _client(clone)
    await ensure_clone(git, str(world.bare))
    await ensure_clone(git, str(world.bare))  # 두 번째는 아무 일도 하지 않는다
    (clone / ".git" / "marker").write_text("m", encoding="utf-8")
    await ensure_clone(git, str(world.bare))
    assert (clone / ".git" / "marker").exists()
    link = clone / "wiki" / "link.md"
    assert not link.is_symlink()
    assert link.is_file()
    assert link.read_text(encoding="utf-8") == LINK_TARGET
    assert (await git.run(["config", "--get", "core.symlinks"])).text.strip() == "false"
    assert (await git.run(["config", "--get", "--bool", "core.symlinks"])).text.strip() == "false"


async def test_ensure_clone_into_an_empty_existing_dir(world: World, tmp_dir: Path) -> None:
    clone = tmp_dir / "empty"
    clone.mkdir()
    git = _client(clone)
    await ensure_clone(git, str(world.bare))
    assert (clone / "wiki" / "index.md").is_file()
    assert await rev_parse(git, "HEAD") == world.main


async def test_disable_symlinks_flattens_existing_links(world: World, tmp_dir: Path) -> None:
    """S1: 링크로 풀린 예전 clone도 `disable_symlinks` 뒤 일반 파일이 된다. `.git/`은 그대로."""
    dest = tmp_dir / "old"
    await _client(tmp_dir).run(["clone", "-q", "--", str(world.bare), str(dest)])
    git = _client(dest)
    link = dest / "wiki" / "link.md"
    assert link.is_symlink()
    os.symlink("/nonexistent/target", dest / "untracked-link")
    os.symlink("wiki", dest / "dir-link")
    os.symlink("somewhere", dest / ".git" / "inner-link")

    await disable_symlinks(git)
    assert (await git.run(["config", "--get", "core.symlinks"])).text.strip() == "false"
    assert not os.path.lexists(link)
    assert not os.path.lexists(dest / "untracked-link")
    assert not os.path.lexists(dest / "dir-link")
    assert (dest / ".git" / "inner-link").is_symlink()
    assert (dest / "wiki" / "index.md").is_file()  # 링크가 아닌 파일은 그대로

    await reset_branch_to(git, "main", "origin/main")
    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == LINK_TARGET
    assert (dest / ".git" / "inner-link").is_symlink()
    assert await changed_paths(git) == []


# ---------------------------------------------------------------- 되돌리기 보호 장치

GuardedCall = Callable[[GitClient], Awaitable[object]]
# 작업 사본을 받는 모든 공개 연산(`ensure_clone` 제외: 폴더를 새로 만든다). 키는 함수 이름이다.
GUARDED: dict[str, GuardedCall] = {
    "disable_symlinks": lambda g: disable_symlinks(g),
    "checkout_detached": lambda g: checkout_detached(g, "HEAD"),
    "reset_branch_to": lambda g: reset_branch_to(g, "main", "HEAD"),
    "discard_changes": lambda g: discard_changes(g),
    "commit_all": lambda g: commit_all(g, "mine"),
    "commit_paths": lambda g: commit_paths(g, ["a.txt"], "mine"),
    "set_remote_url": lambda g: set_remote_url(g, "/nonexistent/probe.git"),
    "remote_url": lambda g: remote_url(g),
    "fetch_branch": lambda g: fetch_branch(g, "main"),
    "push": lambda g: push(g, "HEAD", "probe"),
    "rev_parse": lambda g: rev_parse(g, "HEAD"),
    "try_rev_parse": lambda g: try_rev_parse(g, "HEAD"),
    "first_parent_log": lambda g: first_parent_log(g, None, "HEAD"),
    "commit_date": lambda g: commit_date(g, "HEAD"),
    "author_names": lambda g: author_names(g, "HEAD", SINCE),
    "changed_paths": lambda g: changed_paths(g),
    "is_ancestor": lambda g: is_ancestor(g, "HEAD", "HEAD"),
    "diff_names": lambda g: diff_names(g, "HEAD", "HEAD", ["wiki"]),
    "numstat": lambda g: numstat(g, "HEAD", "HEAD", "wiki"),
    "diff_text": lambda g: diff_text(g, None, "HEAD", 1000),
}


def test_every_public_worktree_op_has_a_guard_probe() -> None:
    """새 공개 async 연산이 생기면 이 목록(과 보호 장치)에 넣지 않고는 통과하지 못한다."""
    public = {
        name
        for name, member in vars(ops).items()
        if inspect.iscoroutinefunction(member)
        and member.__module__ == ops.__name__
        and not name.startswith("_")
    }
    assert public - {"ensure_clone"} == set(GUARDED)


@dataclass(frozen=True)
class Outer:
    root: Path
    bare: Path


@pytest.fixture(scope="module")
def outer_repo() -> Iterator[Outer]:
    """바깥 저장소(고친 추적 파일·추적 안 하는 파일·원격 `origin`). 보호 장치가 지켜야 한다."""
    base = make_tmp_dir("twe-outer-")
    root = base / "outer"
    bare = base / "o.git"

    async def build() -> None:
        await seed_bare(bare, root, {"keep.txt": "original\n"})
        write_tree(root, {"keep.txt": "changed\n", "untracked.txt": "u\n", "inner/x.txt": "x\n"})
        os.symlink("keep.txt", root / "outer-link")

    try:
        asyncio.run(build())
        yield Outer(root, bare)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _tree_snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _outer_state(outer: Outer) -> dict[str, object]:
    """바깥 저장소가 건드려졌는지 비교할 스냅숏(ref·설정·FETCH_HEAD·색인·작업 파일)."""
    git_dir = outer.root / ".git"
    return {
        "refs": _tree_snapshot(git_dir / "refs"),
        "head": (git_dir / "HEAD").read_bytes(),
        "bare_refs": _tree_snapshot(outer.bare / "refs"),
        "config": (git_dir / "config").read_bytes(),
        "index": (git_dir / "index").read_bytes(),
        "fetch_head": os.path.lexists(git_dir / "FETCH_HEAD"),
        "keep": (outer.root / "keep.txt").read_text(encoding="utf-8"),
        "untracked": (outer.root / "untracked.txt").exists(),
        "link": os.path.islink(outer.root / "outer-link"),
        "inner": (outer.root / "inner" / "x.txt").exists(),
    }


def _make_bad_dirs(outer: Outer) -> dict[str, Path]:
    """바깥 저장소 안에 보호 장치가 거부해야 하는 폴더들을 만든다."""
    cases = outer.root / "inner"
    dirs = {
        name: cases / name
        for name in ("nogit", "emptygit", "linkgit", "filegit", "headlink", "headdir", "garbage")
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
        (d / "file.txt").write_text("f\n", encoding="utf-8")
        os.symlink("file.txt", d / "link-in-case")
    (dirs["emptygit"] / ".git").mkdir()
    os.symlink(outer.root / ".git", dirs["linkgit"] / ".git")
    (dirs["filegit"] / ".git").write_text(f"gitdir: {outer.root / '.git'}\n", encoding="utf-8")
    (dirs["headlink"] / ".git").mkdir()
    os.symlink(outer.root / ".git" / "HEAD", dirs["headlink"] / ".git" / "HEAD")
    (dirs["headdir"] / ".git" / "HEAD").mkdir(parents=True)
    (dirs["garbage"] / ".git").mkdir()
    (dirs["garbage"] / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")
    return dirs


@pytest.mark.parametrize("name", sorted(GUARDED))
async def test_worktree_ops_refuse_a_dir_without_a_real_repo(name: str, outer_repo: Outer) -> None:
    """`.git`이 없거나 링크·파일·빈 폴더이거나 `HEAD`가 링크·폴더·쓰레기면 거부한다.

    읽기·push·fetch도 마찬가지다 — 바깥 저장소의 데이터를 돌려주거나 바꾸지 않는다.
    """
    before = _outer_state(outer_repo)
    dirs = _make_bad_dirs(outer_repo)
    try:
        for label, d in dirs.items():
            with pytest.raises(CloneStateError):
                await GUARDED[name](_client(d))
            assert (d / "file.txt").exists(), label
            assert os.path.islink(d / "link-in-case"), label
        assert _outer_state(outer_repo) == before
    finally:
        for d in dirs.values():
            shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("name", sorted(GUARDED))
async def test_worktree_ops_do_not_walk_up_from_a_broken_dot_git(
    name: str, outer_repo: Outer
) -> None:
    """모양만 정상인 깨진 `.git`에서도 git이 바깥 저장소로 올라가지 않는다(`--git-dir` 명시)."""
    before = _outer_state(outer_repo)
    broken = outer_repo.root / "inner" / "broken"
    (broken / ".git").mkdir(parents=True)
    (broken / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (broken / "file.txt").write_text("f\n", encoding="utf-8")
    os.symlink("file.txt", broken / "link-in-case")
    try:
        if name in {
            "try_rev_parse",
            "remote_url",
        }:  # "없음"(`None`)으로 답하는 연산: 바깥 값이 아니다
            assert await GUARDED[name](_client(broken)) is None
        else:
            with pytest.raises(GitError):
                await GUARDED[name](_client(broken))
        assert (broken / "file.txt").exists()
        assert os.path.islink(broken / "link-in-case")
        assert _outer_state(outer_repo) == before
    finally:
        shutil.rmtree(broken, ignore_errors=True)


@pytest.mark.parametrize("name", sorted(GUARDED))
async def test_worktree_ops_refuse_a_cwd_that_is_a_symlink_to_a_real_clone(
    name: str, outer_repo: Outer, tmp_dir: Path
) -> None:
    """`repos/<이름> -> 바깥 clone`이면 거부한다(`lstat(.git)`은 링크를 따라가므로 따로 본다)."""
    before = _outer_state(outer_repo)
    link = tmp_dir / "repos" / "worker"
    link.parent.mkdir(parents=True)
    os.symlink(outer_repo.root, link)
    with pytest.raises(CloneStateError):
        await GUARDED[name](_client(link))
    dangling = tmp_dir / "repos" / "dangling"
    os.symlink(tmp_dir / "missing", dangling)
    with pytest.raises(CloneStateError):
        await GUARDED[name](_client(dangling))
    assert _outer_state(outer_repo) == before


async def test_ensure_clone_refuses_a_cwd_that_is_a_symlink(
    outer_repo: Outer, tmp_dir: Path
) -> None:
    before = _outer_state(outer_repo)
    (tmp_dir / "repos").mkdir()
    os.symlink(outer_repo.root, tmp_dir / "repos" / "worker")
    os.symlink(tmp_dir / "missing", tmp_dir / "repos" / "dangling")
    os.symlink(tmp_dir / "repos", tmp_dir / "repos-link")
    git, script = _fake_client(tmp_dir, tmp_dir / "repos" / "worker")
    for name in ("worker", "dangling"):
        with pytest.raises(CloneStateError):
            await ensure_clone(git.at(tmp_dir / "repos" / name), "https://example.com/r.git")
    assert fake_git_calls(script) == []
    assert _outer_state(outer_repo) == before


async def test_a_symlinked_ancestor_alias_is_still_accepted(tmp_dir: Path) -> None:
    """macOS의 `/var` 같은 별칭 조상은 허용한다 — 마지막 구성요소만 링크가 아니면 된다."""
    real = tmp_dir / "real"
    await init_repo(real / "clone", {"a.txt": "a"})
    os.symlink(real, tmp_dir / "alias")
    git = _client(tmp_dir / "alias" / "clone")
    assert ops.is_sha(await rev_parse(git, "HEAD"))
    await ensure_clone(git, "https://example.com/r.git")


async def test_worktree_ops_refuse_a_missing_dir(tmp_dir: Path) -> None:
    git, script = _fake_client(tmp_dir, tmp_dir / "missing")
    for call in GUARDED.values():
        with pytest.raises(CloneStateError):
            await call(git)
    assert fake_git_calls(script) == []


async def test_guard_accepts_detached_head_and_sha_files(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    sha = await rev_parse(git, "HEAD")
    await checkout_detached(git, sha)
    assert (git.cwd / ".git" / "HEAD").read_text(encoding="utf-8").strip() == sha
    assert await changed_paths(git) == []  # 분리된 HEAD(SHA만 든 파일)도 통과한다


# ---------------------------------------------------------------- clone·fetch·checkout·rev-parse


async def test_clone_fetch_checkout_rev_parse(tmp_dir: Path) -> None:
    bare = tmp_dir / "r.git"
    work = await seed_bare(bare, tmp_dir / "w", {"a.txt": "1"})
    clone = tmp_dir / "clone"
    git = _client(clone)
    await ensure_clone(git, str(bare))
    next_sha = await commit_files(work, {"a.txt": "2"}, "second")
    await work.run(["push", "-q", "origin", "main"])
    await fetch_branch(git, "main")
    await checkout_detached(git, "origin/main")
    assert await rev_parse(git, "HEAD") == next_sha
    assert await rev_parse(git, next_sha) == next_sha
    assert await try_rev_parse(git, "origin/main") == next_sha
    assert await try_rev_parse(git, "b" * 40) is None
    assert await try_rev_parse(git, "nope-branch") is None
    with pytest.raises(GitError):
        await rev_parse(git, "b" * 40)
    with pytest.raises(GitError):
        await fetch_branch(git, "no-such-branch")
    assert (clone / "a.txt").read_text(encoding="utf-8") == "2"


async def test_remote_url_roundtrip(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    assert await remote_url(git) is None
    await set_remote_url(git, str(tmp_dir / "one.git"))
    assert await remote_url(git) == str(tmp_dir / "one.git")
    await set_remote_url(git, str(tmp_dir / "two.git"))
    assert await remote_url(git) == str(tmp_dir / "two.git")


# ---------------------------------------------------------------- 로그


async def test_first_parent_log_sees_merges_not_feature_commits(history: History) -> None:
    git = history.git
    log = await first_parent_log(git, history.base, history.head)
    assert [e.subject for e in log] == [
        "Merge pull request #3 from feat",
        "직접 커밋",
        "wiki 수정",
    ]
    assert [e.sha for e in log] == [history.merge, history.direct, history.head]
    assert all(e.author == "tester" for e in log)
    full = await first_parent_log(git, None, history.head)
    assert [e.subject for e in full] == [
        "init",
        "Merge pull request #3 from feat",
        "직접 커밋",
        "wiki 수정",
    ]
    assert await first_parent_log(git, history.head, history.head) == []
    from team_wiki_engine.util.time import parse_instant

    assert all(parse_instant(e.committed_at) is not None for e in log)


async def test_commit_date_and_author_names(history: History) -> None:
    when = await commit_date(history.git, history.head)
    assert when.tzinfo is not None
    assert abs(datetime.now(UTC) - when) < timedelta(days=1)
    names = await author_names(history.git, "HEAD", SINCE)
    assert names[0] == "tester"
    assert all(name == "tester" and "\n" not in name for name in names)
    assert len(names) == 5  # init, feature work, merge, 직접 커밋, wiki 수정
    assert await author_names(history.git, "HEAD", datetime.now(UTC) + timedelta(days=1)) == []


class _Stub(GitClient):
    """`run`을 가로채 고정 응답(또는 예외)을 돌려주는 클라이언트."""

    def __init__(
        self, cwd: Path, handler: Callable[[Sequence[str], dict[str, Any]], GitResult]
    ) -> None:
        _mark_repo(cwd)
        super().__init__(cwd)
        self.handler = handler
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    async def run(self, args: Sequence[str], **kwargs: Any) -> GitResult:
        self.calls.append((list(args), kwargs))
        return self.handler(args, kwargs)


async def test_commit_date_rejects_unparseable_output(tmp_dir: Path) -> None:
    stub = _Stub(tmp_dir, lambda args, kw: GitResult(0, b"garbage\n", ""))
    with pytest.raises(ValueError):
        await commit_date(stub, "a" * 40)


# ---------------------------------------------------------------- 상태·커밋


async def test_changed_paths_korean_space_and_rename(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"wiki/old name.md": "x"})
    await git.run(["mv", "wiki/old name.md", "wiki/새 이름.md"])
    write_tree(git.cwd, {"raw/회의 록.md": "r"})
    assert _sorted(await changed_paths(git)) == [
        "raw/회의 록.md",
        "wiki/old name.md",
        "wiki/새 이름.md",
    ]


async def test_changed_paths_includes_ignored_and_deleted(tmp_dir: Path) -> None:
    """G5: `.gitignore`에 걸린 파일과 `.DS_Store`, 삭제된 파일도 목록에 든다."""
    git = await init_repo(
        tmp_dir / "r",
        {".gitignore": "raw/secret.txt\n.DS_Store\n", "wiki/gone.md": "g", "wiki/keep.md": "k"},
    )
    assert await changed_paths(git) == []
    (git.cwd / "wiki" / "gone.md").unlink()
    write_tree(git.cwd, {"raw/secret.txt": "s", "wiki/.DS_Store": "d"})
    assert _sorted(await changed_paths(git)) == [
        "raw/secret.txt",
        "wiki/.DS_Store",
        "wiki/gone.md",
    ]


async def test_commit_all_returns_none_when_unchanged_and_uses_bot_author(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    assert await commit_all(git, "nothing") is None
    write_tree(git.cwd, {"b.txt": "b"})
    sha = await commit_all(git, "봇 커밋")
    assert sha is not None
    assert ops.is_sha(sha)
    assert await rev_parse(git, "HEAD") == sha
    shown = await git.run(["log", "-1", "--format=%an <%ae>|%cn <%ce>|%s"])
    bot = "team-wiki-bot <team-wiki-bot@users.noreply.github.com>"
    assert shown.text.strip() == f"{bot}|{bot}|봇 커밋"
    assert await commit_all(git, "again") is None


async def test_commit_paths_commits_only_the_given_paths(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a", "b.txt": "b", "wiki/x.md": "x"})
    write_tree(git.cwd, {"a.txt": "a2", "b.txt": "b2", "new.txt": "n", "wiki/y.md": "y"})
    sha = await commit_paths(git, ["a.txt"], "a만")
    assert sha is not None
    assert await rev_parse(git, "HEAD") == sha
    shown = await git.run(["show", "--name-only", "--format=%an <%ae>|%s", "HEAD"])
    lines = shown.text.split("\n")
    assert lines[0] == "team-wiki-bot <team-wiki-bot@users.noreply.github.com>|a만"
    assert [line for line in lines[1:] if line] == ["a.txt"]
    assert _sorted(await changed_paths(git)) == ["b.txt", "new.txt", "wiki/y.md"]  # 나머지는 그대로


async def test_commit_paths_ignores_other_staged_files(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a", "b.txt": "b"})
    write_tree(git.cwd, {"a.txt": "a2", "b.txt": "b2"})
    await git.run(["add", "b.txt"])  # 이미 올려 둔 다른 파일
    assert await commit_paths(git, ["a.txt"], "a만") is not None
    shown = await git.run(["show", "--name-only", "--format=", "HEAD"])
    assert shown.text.split() == ["a.txt"]
    assert await changed_paths(git) == ["b.txt"]


async def test_commit_paths_returns_none_when_the_paths_are_unchanged(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a", "b.txt": "b"})
    write_tree(git.cwd, {"b.txt": "b2"})  # 다른 파일만 바뀜
    assert await commit_paths(git, ["a.txt"], "nothing") is None
    assert await changed_paths(git) == ["b.txt"]


async def test_commit_paths_takes_paths_literally(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.md": "a"})
    write_tree(git.cwd, {"a.md": "a2", "*.md": "star"})
    assert await commit_paths(git, ["*.md"], "별") is not None
    shown = await git.run(["show", "--name-only", "--format=", "HEAD"])
    assert shown.text.split() == ["*.md"]  # `a.md`는 글롭에 걸리지 않는다
    assert await changed_paths(git) == ["a.md"]


async def test_commit_paths_commits_a_deletion(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a", "b.txt": "b"})
    (git.cwd / "a.txt").unlink()
    assert await commit_paths(git, ["a.txt"], "삭제") is not None
    assert (await git.run(["ls-files"])).text.split() == ["b.txt"]


@pytest.mark.parametrize("paths", [[], [""], ["a\0b"], ["a\udc80"]])
async def test_commit_paths_rejects_bad_paths(tmp_dir: Path, paths: list[str]) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    with pytest.raises(ValueError):
        await commit_paths(git, paths, "x")


async def test_commit_paths_rejects_bad_messages(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    with pytest.raises(ValueError):
        await commit_paths(git, ["a.txt"], "")
    with pytest.raises(ValueError):
        await commit_paths(git, ["a.txt"], "x\0y")


async def test_commit_paths_refuses_a_nested_repository(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    await init_repo(git.cwd / "inner", {"i.txt": "i"})
    before = await rev_parse(git, "HEAD")
    with pytest.raises(CloneStateError):
        await commit_paths(git, ["inner"], "중첩")
    assert await rev_parse(git, "HEAD") == before


async def test_commit_paths_refuses_a_symlink(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    os.symlink("/etc/passwd", git.cwd / "link.txt")
    before = await rev_parse(git, "HEAD")
    with pytest.raises(CloneStateError):
        await commit_paths(git, ["link.txt"], "링크")
    assert await rev_parse(git, "HEAD") == before
    assert (await git.run(["diff", "--cached", "--name-only"])).text == ""  # 올려 둔 것도 되돌렸다


# ---------------------------------------------------------------- push·diff


async def test_push_ancestor_diff_names_numstat_and_non_fast_forward(
    world: World, tmp_dir: Path
) -> None:
    git = _client(tmp_dir / "c")
    await ensure_clone(git, str(world.bare))
    await reset_branch_to(git, "sync/ok", "origin/main")
    base = await rev_parse(git, "HEAD")
    write_tree(git.cwd, {"wiki/a.md": "a\nb\nc\n", "wiki/b.md": "b\n", "raw/r.md": "r\n"})
    (git.cwd / "wiki" / "image.bin").write_bytes(b"\x00\x01\x02binary")
    head = await commit_all(git, "change")
    assert head is not None
    await push(git, "HEAD", "sync/ok")
    bare = _client(world.bare)
    assert await _ref(bare, "refs/heads/sync/ok") == head

    assert await is_ancestor(git, base, head) is True
    assert await is_ancestor(git, head, base) is False
    assert await is_ancestor(git, head, head) is True
    assert _sorted(await diff_names(git, base, head, ["wiki"])) == [
        "wiki/a.md",
        "wiki/b.md",
        "wiki/image.bin",
    ]
    assert _sorted(await diff_names(git, base, head, ["wiki", "raw"])) == [
        "raw/r.md",
        "wiki/a.md",
        "wiki/b.md",
        "wiki/image.bin",
    ]
    assert await diff_names(git, base, head, [":(top)wiki"]) == []  # 경로 마법 문자는 글자 그대로
    assert await numstat(git, base, head, "wiki/") == NumStat(files=3, additions=4, deletions=0)
    assert await numstat(git, base, base, "wiki/") == NumStat(0, 0, 0)
    assert _sorted(await diff_names(git, EMPTY_TREE, head, ["raw"])) == ["raw/r.md"]

    # 빨리 감기가 아니면 실패하고 원격은 그대로다
    other = _client(tmp_dir / "c2")
    await ensure_clone(other, str(world.bare))
    await reset_branch_to(other, "sync/nff", "origin/main")
    write_tree(other.cwd, {"x.md": "first\n"})
    first = await commit_all(other, "first")
    assert first is not None
    await push(other, "HEAD", "sync/nff")
    await reset_branch_to(git, "sync/nff", "origin/main")
    write_tree(git.cwd, {"y.md": "second\n"})
    second = await commit_all(git, "second")
    assert second is not None and second != first
    with pytest.raises(GitError):
        await push(git, "HEAD", "sync/nff")
    assert await _ref(bare, "refs/heads/sync/nff") == first


async def test_diff_names_and_numstat_on_history(history: History) -> None:
    git = history.git
    assert _sorted(await diff_names(git, history.base, history.head, ["wiki"])) == [
        "wiki/a.md",
        "wiki/b.md",
        "wiki/한글 문서.md",
    ]
    assert await diff_names(git, history.base, history.head, []) != []
    assert await numstat(git, history.base, history.head, "wiki") == NumStat(3, 5, 0)
    assert await numstat(git, history.base, history.head, "한글") == NumStat(1, 1, 0)
    # 경로 마법 문자는 글자 그대로 본다(`--literal-pathspecs`)
    assert await numstat(git, history.base, history.head, ":(top)wiki") == NumStat(0, 0, 0)
    assert await numstat(git, history.base, history.head, ":(glob)wiki/*.md") == NumStat(0, 0, 0)


# ---------------------------------------------------------------- diff_text


async def test_diff_text_full_then_truncated_to_a_file_list(history: History) -> None:
    git = history.git
    with pytest.raises(GitError):  # 대조군: 플래그 없이는 이 저장소의 diff가 실패한다
        await git.run(["diff", history.base, history.head, "--"])
    full = await diff_text(git, history.base, history.head, 100_000)
    assert full == DiffText(text=full.text, truncated=False)
    assert "한글/파일.md" in full.text
    assert "\\355" not in full.text

    cut = await diff_text(git, history.base, history.head, 200)
    assert cut.truncated is True
    assert cut.text.startswith("(diff가 200바이트를 넘어 파일 목록만 적었습니다.")
    assert "필요한 파일은 코드 사본에서 직접 읽으세요.)\n\n" in cut.text
    assert "한글/파일.md" in cut.text  # 8진 이스케이프(`\355\225…`)가 아니다
    assert "\\355" not in cut.text
    assert "wiki/한글 문서.md" in cut.text

    from_empty = await diff_text(git, None, history.head, 100_000)
    assert "a.txt" in from_empty.text and not from_empty.truncated
    at_boundary = await diff_text(git, history.base, history.base, 0)
    assert at_boundary == DiffText("", False)


async def test_diff_text_falls_back_when_even_the_stat_is_too_large(tmp_dir: Path) -> None:
    def handler(args: Sequence[str], kwargs: dict[str, Any]) -> GitResult:
        raise GitOutputTooLarge("too large")

    stub = _Stub(tmp_dir, handler)
    result = await diff_text(stub, None, "HEAD", 1234)
    assert result.truncated is True
    assert "(diff가 1234바이트를 넘어 파일 목록만 적었습니다." in result.text
    assert result.text.endswith("(파일 목록도 256KB를 넘어 생략)")
    first, second = stub.calls
    assert first[0][2:] == [
        "-c",
        "core.quotepath=false",
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        EMPTY_TREE,
        "HEAD",
        "--",
    ]
    assert first[1]["max_output_bytes"] == 1234
    assert second[0][2:5] == ["-c", "core.quotepath=false", "diff"]
    assert "--stat=200" in second[0] and "--no-color" in second[0]
    assert second[1]["max_output_bytes"] == 256 * 1024


# ---------------------------------------------------------------- 쓰레기 없이 되돌리기


async def test_discarding_leaves_no_junk_including_ignored_files(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a", ".gitignore": ".DS_Store\n*.log\n"})

    def junk() -> None:
        write_tree(
            git.cwd,
            {"a.txt": "changed", "new.txt": "n", ".DS_Store": "d", "sub/x.log": "l"},
        )
        (git.cwd / "empty-dir").mkdir(exist_ok=True)

    junk()
    assert _sorted(await changed_paths(git)) != []
    await discard_changes(git)
    assert await changed_paths(git) == []
    assert (git.cwd / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (git.cwd / ".DS_Store").exists()
    assert not (git.cwd / "sub").exists()

    junk()
    await checkout_detached(git, "HEAD")
    assert await changed_paths(git) == []
    assert (git.cwd / "a.txt").read_text(encoding="utf-8") == "a"

    junk()
    await reset_branch_to(git, "work/x", "HEAD")
    assert await changed_paths(git) == []
    assert (await git.run(["branch", "--show-current"])).text.strip() == "work/x"


async def test_clean_only_touches_the_given_worktree(tmp_dir: Path) -> None:
    """작업 사본 안의 하위 저장소가 아닌 바깥 형제 폴더의 파일은 그대로다."""
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    write_tree(tmp_dir, {"sibling/keep.txt": "k"})
    write_tree(git.cwd, {"junk.txt": "j"})
    await discard_changes(git)
    assert (tmp_dir / "sibling" / "keep.txt").exists()
    assert not (git.cwd / "junk.txt").exists()


# ---------------------------------------------------------------- 리뷰 반영(1차)


async def test_log_uses_nul_separators_so_forged_records_cannot_appear(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    forged = "f" * 40
    subject = f"evil\x1e{forged}\x1f2026-01-01T00:00:00+00:00\x1fmallory\x1fforged \x1f | 한글"
    write_tree(git.cwd, {"b.txt": "b"})
    await git.run(["add", "-A"])
    await git.run(["commit", "-q", "--author", "x\x1ey\x1fz <x@example.com>", "-m", subject])
    log = await first_parent_log(git, None, "HEAD")
    assert len(log) == 2
    assert all(e.sha != forged for e in log)
    assert log[1].subject == subject
    assert log[1].author == "x\x1ey\x1fz"
    names = await author_names(git, "HEAD", SINCE)
    assert names == ["x\x1ey\x1fz", "tester"]


async def test_log_and_show_pass_no_show_signature_and_dashdash(tmp_dir: Path) -> None:
    git, script = _fake_client(tmp_dir, repo=True)
    with pytest.raises(GitError):  # 가짜 git은 log·show를 모른다
        await first_parent_log(git, "a" * 40, "b" * 40)
    with pytest.raises(GitError):
        await commit_date(git, "a" * 40)
    with pytest.raises(GitError):
        await author_names(git, "HEAD", SINCE)
    log_call, show_call, names_call = (call[2:] for call in fake_git_calls(script))
    assert log_call[0] == "log" and "--no-show-signature" in log_call
    assert "-z" in log_call and log_call[-1] == "--"
    assert log_call[-2] == f"{'a' * 40}..{'b' * 40}"
    assert show_call[0] == "show" and "--no-show-signature" in show_call
    assert names_call[0] == "log" and "--no-show-signature" in names_call
    assert "-z" in names_call and names_call[-1] == "--"


@pytest.mark.parametrize(
    ("since", "expected"),
    [
        (datetime(2026, 10, 7, 0, 0, 0), "2026-10-07T00:00:00+00:00"),  # naive = UTC(P9)
        (datetime(2026, 10, 7, 0, 0, 0, tzinfo=UTC), "2026-10-07T00:00:00+00:00"),
        (
            datetime(2026, 10, 7, 9, 0, 0, tzinfo=timezone(timedelta(hours=9))),
            "2026-10-07T00:00:00+00:00",
        ),
        (datetime(2026, 10, 7, 0, 0, 0, 123456, tzinfo=UTC), "2026-10-07T00:00:00+00:00"),
    ],
)
async def test_author_names_passes_an_unambiguous_utc_since(
    since: datetime, expected: str, tmp_dir: Path
) -> None:
    git, script = _fake_client(tmp_dir, repo=True)
    with pytest.raises(GitError):
        await author_names(git, "HEAD", since)
    [call] = fake_git_calls(script)
    assert f"--since={expected}" in call


async def test_author_names_naive_since_is_not_machine_local(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    now = datetime.now(UTC).replace(tzinfo=None)  # naive, UTC 벽시계
    assert await author_names(git, "HEAD", now - timedelta(hours=1)) == ["tester"]
    assert await author_names(git, "HEAD", now + timedelta(hours=1)) == []


async def test_commit_all_too_large_message_stages_nothing(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    head = await rev_parse(git, "HEAD")
    write_tree(git.cwd, {"b.txt": "b"})
    with pytest.raises(ValueError):
        await commit_all(git, "x" * 65_537)
    assert (await git.run(["diff", "--cached", "--quiet"])).code == 0  # 아무것도 올리지 않았다
    assert await rev_parse(git, "HEAD") == head
    assert await commit_all(git, "x" * 65_536) is not None  # 상한 그대로는 허용


async def test_discard_changes_removes_nested_repos_with_double_force(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    nested = await init_repo(git.cwd / "nested", {"n.txt": "n"})
    assert (git.cwd / "nested" / ".git").is_dir() and nested.cwd.is_dir()
    await discard_changes(git)
    assert not (git.cwd / "nested").exists()  # `clean -ff`가 아니면 중첩 저장소는 남는다


async def test_commit_all_refuses_nested_repo_gitlinks(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    head = await rev_parse(git, "HEAD")
    await init_repo(git.cwd / "nested", {"n.txt": "n"})
    write_tree(git.cwd, {"b.txt": "b"})
    with pytest.raises(CloneStateError):
        await commit_all(git, "with nested")
    assert await rev_parse(git, "HEAD") == head
    assert (await git.run(["diff", "--cached", "--quiet"])).code == 0  # 올려 둔 것도 되돌렸다
    modes = (await git.run(["ls-tree", "-r", "HEAD"])).text
    assert "160000" not in modes
    await discard_changes(git)
    write_tree(git.cwd, {"b.txt": "b"})
    assert await commit_all(git, "clean now") is not None


async def test_commit_all_never_runs_the_configured_gpg_program(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    marker = tmp_dir / "gpg-was-run"
    program = tmp_dir / "fake-gpg"
    program.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
    program.chmod(0o755)
    with (git.cwd / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(f"[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = {program}\n")
    write_tree(git.cwd, {"b.txt": "b"})
    assert await commit_all(git, "unsigned") is not None
    assert not marker.exists()


async def test_push_and_log_argv_have_dashdash(tmp_dir: Path) -> None:
    git, script = _fake_client(tmp_dir, repo=True)
    with pytest.raises(GitError):
        await push(git, "HEAD", "sync/x")
    with pytest.raises(GitError):
        await fetch_branch(git, "main")
    push_call, fetch_call = (call[2:] for call in fake_git_calls(script))
    assert push_call == ["push", "-q", "--no-verify", "--", "origin", "HEAD:refs/heads/sync/x"]
    assert fetch_call[:6] == ["fetch", "-q", "--prune", "--no-tags", "--", "origin"]


async def test_set_remote_url_rejects_userinfo_so_tokens_never_reach_git_config(
    tmp_dir: Path,
) -> None:
    git = await init_repo(tmp_dir / "r", {"a.txt": "a"})
    with pytest.raises(ValueError) as caught:
        await set_remote_url(git, "https://x-access-token:ghp_secret_1234567@github.com/o/n.git")
    assert "ghp_secret" not in str(caught.value)
    assert await remote_url(git) is None
    assert "ghp_secret" not in (git.cwd / ".git" / "config").read_text(encoding="utf-8")


# ------------------------------------------------ 읽기 전용 호출은 선택적 락을 쥐지 않는다


async def test_changed_paths_carries_no_optional_locks(tmp_dir: Path) -> None:
    git, script = _fake_client(tmp_dir, repo=True)
    with pytest.raises(GitError):  # 가짜 git은 모르는 명령이라 실패한다 — argv만 본다
        await ops.changed_paths(git)
    argv = fake_git_calls(script)[0]
    assert argv[2:] == [
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "-uall",
        "--ignored=matching",
    ]
