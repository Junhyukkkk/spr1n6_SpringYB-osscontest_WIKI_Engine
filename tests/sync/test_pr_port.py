"""PR 포트(GitHub · 로컬 파일)와 추적 파일.

로컬 포트는 임시 폴더의 실제 bare 저장소로, 사람이 망가뜨린 파일과 입력 검증은 가짜 git의
호출 기록(`fake_git_calls(...) == []`)으로 확인한다. GitHub 포트는 `FakeGitHub`로만 돈다.
"""

import asyncio
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_args

import pytest
import pytest_asyncio
from pydantic import ValidationError

from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import (
    CloneStateError,
    commit_all,
    ensure_clone,
    fetch_branch,
    push,
    reset_branch_to,
)
from team_wiki_engine.github.client import GitHubError, RepoRef
from team_wiki_engine.metrics.record import MAX_STATE_JSON, STATE_FILES
from team_wiki_engine.sync.pr_port import (
    LINT_FAILED_LABEL,
    LOCAL_PR_NAME,
    MAX_LOCAL_PR_BYTES,
    SYNC_LABELS,
    GitHubPrPort,
    LocalPrPort,
    OpenPrRequest,
    PrOutcome,
    PrPort,
    TrackedPr,
    read_tracking,
    write_tracking,
)
from team_wiki_engine.sync.types import SyncKind
from team_wiki_engine.wiki.frontmatter import parse_frontmatter, serialize_frontmatter
from tests.helpers import write_tree
from tests.helpers.fake_git import fake_git_calls, write_fake_git
from tests.helpers.git import commit_files, seed_bare
from tests.helpers.github import FakeGitHub, pull_detail, pull_summary

NOW = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
RUN_ID = "20261007-0900"
SHA_A = "a" * 40
OPEN = PrOutcome(state="open", final_head_sha=None, closed_at=None)
REPO = RepoRef("me", "team-wiki-data")


def _now() -> datetime:
    return NOW


def _request(sha: str, **extra: Any) -> OpenPrRequest:
    fields: dict[str, Any] = {
        "kind": "code",
        "run_id": RUN_ID,
        "branch": "sync/code-1",
        "base": "main",
        "title": "위키 코드 반영",
        "body": "PR 본문입니다",
        "lint_failed": False,
        "bot_head_sha": sha,
    }
    fields.update(extra)
    return OpenPrRequest(**fields)


def _tracked(pr_id: str = "1", branch: str = "b", kind: SyncKind = "code") -> TrackedPr:
    return TrackedPr(id=pr_id, kind=kind, branch=branch, bot_head_sha=SHA_A)


def _raw_tracked(pr_id: str) -> TrackedPr:
    """검증을 거치지 않은 `TrackedPr`(포트의 방어선만 시험한다)."""
    return TrackedPr.model_construct(id=pr_id, kind="code", branch="b", bot_head_sha=SHA_A)


def _mark_repo(directory: Path) -> None:
    """가짜 git 시험용으로 보호 장치를 통과하는 `.git`(폴더 + 정상 모양의 `HEAD`)을 만든다."""
    (directory / ".git").mkdir(parents=True, exist_ok=True)
    (directory / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def _fake_git(root: Path, *, repo: bool = True) -> tuple[GitClient, Path]:
    script = write_fake_git(root / "bin")
    clone = root / "clone"
    if repo:
        _mark_repo(clone)
    else:
        clone.mkdir(parents=True, exist_ok=True)
    return GitClient(clone, git_bin=str(script), allowed_protocols="file"), script


def _fake_local(root: Path) -> tuple[LocalPrPort, Path, Path]:
    """가짜 git을 쓰는 로컬 포트와 PR 폴더, 가짜 git 스크립트."""
    git, script = _fake_git(root)
    directory = root / "local-prs"
    directory.mkdir()
    return LocalPrPort(directory=directory, data_git=git, base="main", now=_now), directory, script


def _pr_text(**fields: Any) -> str:
    meta: dict[str, Any] = {
        "title": "t",
        "kind": "code",
        "branch": "sync/code-1",
        "base": "main",
        "status": "open",
        "bot_head_sha": SHA_A,
        "lint_failed": False,
        "created_at": "2026-10-07T09:00:00+09:00",
    }
    meta.update(fields)
    return serialize_frontmatter(meta, "\n본문\n")


# ---------------------------------------------------------------- 상수·모델


def test_constants() -> None:
    assert isinstance(SYNC_LABELS, MappingProxyType)
    assert dict(SYNC_LABELS) == {"code": "wiki-sync:code", "ingest": "wiki-sync:ingest"}
    assert set(SYNC_LABELS) == set(get_args(SyncKind))
    assert LINT_FAILED_LABEL == "점검 실패"
    assert MAX_LOCAL_PR_BYTES == 1_048_576
    with pytest.raises(TypeError):
        SYNC_LABELS["code"] = "x"  # type: ignore[index]


@pytest.mark.parametrize(
    "name", ["20261007-0900-code.md", "20261007-0900-ingest.md", "00000000-0000-code.md"]
)
def test_local_pr_name_accepts(name: str) -> None:
    assert LOCAL_PR_NAME.fullmatch(name)


@pytest.mark.parametrize(
    "name",
    [
        "20261007-0900-code.md\n",
        "\n20261007-0900-code.md",
        "20261007-0900-other.md",
        "2026107-0900-code.md",
        "20261007-900-code.md",
        "20261007-0900-code.md.tmp",
        ".20261007-0900-code.md.123.tmp",
        "２０２６１００７-0900-code.md",  # 전각 숫자
        "٢٠٢٦١٠٠٧-0900-code.md",  # 아라비아 숫자
        "notes.md",
        "",
    ],
)
def test_local_pr_name_rejects(name: str) -> None:
    assert LOCAL_PR_NAME.fullmatch(name) is None


class TestTrackedPr:
    def test_valid(self) -> None:
        pr = TrackedPr(id="1", kind="ingest", branch="sync/ingest-1", bot_head_sha=SHA_A)
        assert (pr.id, pr.kind, pr.branch, pr.bot_head_sha) == (
            "1",
            "ingest",
            "sync/ingest-1",
            SHA_A,
        )

    def test_frozen(self) -> None:
        with pytest.raises(ValidationError):
            _tracked().id = "2"

    @pytest.mark.parametrize("branch", ["--upload-pack=x", "../x", "", "a b", "x\n", "a" * 201])
    def test_rejects_unsafe_branch(self, branch: str) -> None:
        with pytest.raises(ValidationError):
            TrackedPr(id="1", kind="code", branch=branch, bot_head_sha=SHA_A)

    @pytest.mark.parametrize("sha", ["", "abc", "A" * 40, SHA_A + "\n", "g" * 40, "a" * 41])
    def test_rejects_bad_sha(self, sha: str) -> None:
        with pytest.raises(ValidationError):
            TrackedPr(id="1", kind="code", branch="b", bot_head_sha=sha)

    @pytest.mark.parametrize(
        "pr_id",
        [
            "7; rm",
            "1\u0000",
            "../x",
            "1\n",
            "\uff11\uff12",  # 전각 숫자
            "\u0663",  # 아라비아 숫자
            "",
            "-1",
            "1" * 16,
            f"{RUN_ID}-code.md\n",
            f"../{RUN_ID}-code.md",
            f"{RUN_ID}-other.md",
            f"sub/{RUN_ID}-code.md",
            "x.md",
        ],
    )
    def test_rejects_unsafe_id(self, pr_id: str) -> None:
        with pytest.raises(ValidationError):
            TrackedPr(id=pr_id, kind="code", branch="b", bot_head_sha=SHA_A)

    @pytest.mark.parametrize(
        "pr_id", ["0", "1", "101", "1" * 15, f"{RUN_ID}-code.md", f"{RUN_ID}-ingest.md"]
    )
    def test_accepts_both_id_forms(self, pr_id: str) -> None:
        assert TrackedPr(id=pr_id, kind="code", branch="b", bot_head_sha=SHA_A).id == pr_id

    def test_rejects_bad_kind_and_types(self) -> None:
        with pytest.raises(ValidationError):
            TrackedPr(id="1", kind="x", branch="b", bot_head_sha=SHA_A)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            TrackedPr(id=1, kind="code", branch="b", bot_head_sha=SHA_A)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 추적 파일


class TestTracking:
    def test_roundtrip(self, tmp_dir: Path) -> None:
        assert read_tracking(tmp_dir) == []
        prs = [
            TrackedPr(id="7", kind="ingest", branch="b", bot_head_sha=SHA_A),
            TrackedPr(id=f"{RUN_ID}-code.md", kind="code", branch="sync/x", bot_head_sha="b" * 40),
        ]
        write_tracking(tmp_dir, prs)
        assert read_tracking(tmp_dir) == prs
        raw = json.loads((tmp_dir / STATE_FILES.pr_tracking).read_text(encoding="utf-8"))
        assert raw[0] == {"id": "7", "kind": "ingest", "branch": "b", "bot_head_sha": SHA_A}

    def test_write_empty_and_overwrite(self, tmp_dir: Path) -> None:
        write_tracking(tmp_dir, [_tracked()])
        write_tracking(tmp_dir, [])
        assert read_tracking(tmp_dir) == []

    @pytest.mark.parametrize(
        "content",
        [
            "쓰레기",
            "",
            "{}",
            "[]",
            "null",
            "[[[[",
            '"문자열"',
            "7",
            '{"id": "1"}',
            "[" * 100_000,
            "\x00\x01",
        ],
    )
    def test_garbage_gives_empty(self, tmp_dir: Path, content: str) -> None:
        (tmp_dir / STATE_FILES.pr_tracking).write_text(content, encoding="utf-8")
        assert read_tracking(tmp_dir) == []

    def test_keeps_only_valid_entries(self, tmp_dir: Path) -> None:
        good = {"id": "1", "kind": "code", "branch": "b", "bot_head_sha": SHA_A}
        entries: list[Any] = [
            good,
            {**good, "id": "2", "bot_head_sha": "zzz"},
            {**good, "id": "3", "kind": "x"},
            {**good, "id": "4", "branch": "--upload-pack=x"},
            {**good, "id": 5},
            {"id": "6"},
            {},
            5,
            "문자열",
            None,
            [],
            {**good, "id": "8", "kind": "ingest", "extra": "무시"},
        ]
        (tmp_dir / STATE_FILES.pr_tracking).write_text(json.dumps(entries), encoding="utf-8")
        got = read_tracking(tmp_dir)
        assert [p.id for p in got] == ["1", "8"]
        assert got[1].kind == "ingest"

    def test_entries_with_unsafe_ids_are_dropped(self, tmp_dir: Path) -> None:
        good = {"id": "1", "kind": "code", "branch": "b", "bot_head_sha": SHA_A}
        bad_ids = ["7; rm", "1\u0000", "../x", "1\n", "\uff11", "", f"{RUN_ID}-code.md\n"]
        entries = [{**good, "id": bad} for bad in bad_ids] + [good]
        (tmp_dir / STATE_FILES.pr_tracking).write_text(json.dumps(entries), encoding="utf-8")
        assert [p.id for p in read_tracking(tmp_dir)] == ["1"]

    def test_duplicate_ids_keep_first(self, tmp_dir: Path) -> None:
        good = {"id": "1", "kind": "code", "branch": "b", "bot_head_sha": SHA_A}
        entries = [good, {**good, "branch": "other"}]
        (tmp_dir / STATE_FILES.pr_tracking).write_text(json.dumps(entries), encoding="utf-8")
        assert [p.branch for p in read_tracking(tmp_dir)] == ["b"]

    def test_oversized_file_is_ignored(self, tmp_dir: Path) -> None:
        good = json.dumps({"id": "1", "kind": "code", "branch": "b", "bot_head_sha": SHA_A})
        text = "[" + ",".join([good] * (MAX_STATE_JSON // len(good) + 2)) + "]"
        assert len(text) > MAX_STATE_JSON
        (tmp_dir / STATE_FILES.pr_tracking).write_text(text, encoding="utf-8")
        assert read_tracking(tmp_dir) == []

    def test_many_invalid_entries_do_not_raise(self, tmp_dir: Path) -> None:
        text = "[" + ",".join(["{}"] * 300_000) + "]"
        assert len(text) <= MAX_STATE_JSON
        (tmp_dir / STATE_FILES.pr_tracking).write_text(text, encoding="utf-8")
        assert read_tracking(tmp_dir) == []

    def test_symlink_or_directory_is_not_followed(self, tmp_dir: Path) -> None:
        target = tmp_dir / "elsewhere.json"
        good = {"id": "1", "kind": "code", "branch": "b", "bot_head_sha": SHA_A}
        target.write_text(json.dumps([good]), encoding="utf-8")
        state = tmp_dir / "state"
        state.mkdir()
        (state / STATE_FILES.pr_tracking).mkdir()
        assert read_tracking(state) == []


# ---------------------------------------------------------------- 로컬 포트(실제 git)


@dataclass
class Env:
    root: Path
    seed: GitClient
    data_git: GitClient
    pr_dir: Path
    port: LocalPrPort


@pytest_asyncio.fixture
async def env(tmp_dir: Path) -> Env:
    bare = tmp_dir / "data.git"
    seed = await seed_bare(bare, tmp_dir / "seed", {"raw/a.md": "a"})
    clone = tmp_dir / "data-sync"
    data_git = GitClient(clone, allowed_protocols="file", timeout_s=60.0)
    await ensure_clone(data_git, str(bare))
    await fetch_branch(data_git, "main")
    pr_dir = tmp_dir / "local-prs"
    port = LocalPrPort(directory=pr_dir, data_git=data_git, base="main", now=_now)
    return Env(tmp_dir, seed, data_git, pr_dir, port)


async def _make_branch(env: Env, branch: str) -> str:
    await reset_branch_to(env.data_git, branch, "origin/main")
    write_tree(env.data_git.cwd, {"wiki/a.md": f"{branch}\n"})
    sha = await commit_all(env.data_git, branch)
    assert sha is not None
    await push(env.data_git, "HEAD", branch)
    return sha


def _protocol(port: LocalPrPort) -> PrPort:
    return port  # LocalPrPort가 PrPort 구조를 만족하는지 mypy가 확인한다


class TestLocalPort:
    async def test_open_writes_file_and_finds_it(self, env: Env) -> None:
        sha = await _make_branch(env, "sync/code-1")
        pr = await _protocol(env.port).open(_request(sha))
        assert pr == TrackedPr(
            id="20261007-0900-code.md", kind="code", branch="sync/code-1", bot_head_sha=sha
        )
        text = (env.pr_dir / pr.id).read_text(encoding="utf-8")
        parsed = parse_frontmatter(text)
        assert parsed.error is None
        assert parsed.data == {
            "title": "위키 코드 반영",
            "kind": "code",
            "branch": "sync/code-1",
            "base": "main",
            "status": "open",
            "bot_head_sha": sha,
            "lint_failed": False,
            "created_at": "2026-10-07T09:00:00+09:00",
        }
        assert "PR 본문입니다" in parsed.body
        assert await env.port.find_open("code") == pr.id
        assert await env.port.find_open("ingest") is None
        assert await env.port.outcome(pr) == OPEN
        assert [p.name for p in env.pr_dir.iterdir()] == [pr.id]  # 임시 파일이 남지 않는다

    async def test_merged_when_branch_is_ancestor_of_base(self, env: Env) -> None:
        sha = await _make_branch(env, "sync/code-1")
        pr = await env.port.open(_request(sha))
        await env.seed.run(["fetch", "-q", "origin"])
        await env.seed.run(["merge", "-q", "--no-ff", "-m", "merge", "origin/sync/code-1"])
        await env.seed.run(["push", "-q", "origin", "main"])
        assert await env.port.outcome(pr) == PrOutcome("merged", sha, None)
        assert await env.port.find_open("code") is None

    async def test_human_fix_commit_is_final(self, env: Env) -> None:
        sha = await _make_branch(env, "sync/code-1")
        pr = await env.port.open(_request(sha))
        await env.seed.run(["fetch", "-q", "origin"])
        await env.seed.run(["checkout", "-q", "-b", "review", "origin/sync/code-1"])
        fixed = await commit_files(env.seed, {"wiki/a.md": "사람이 고침\n"}, "human fix")
        await env.seed.run(["push", "-q", "origin", "review:sync/code-1"])
        await env.seed.run(["checkout", "-q", "main"])
        await env.seed.run(["merge", "-q", "--no-ff", "-m", "merge", "review"])
        await env.seed.run(["push", "-q", "origin", "main"])
        assert await env.port.outcome(pr) == PrOutcome("merged", fixed, None)

    async def test_status_rejected(self, env: Env) -> None:
        sha = await _make_branch(env, "sync/code-1")
        pr = await env.port.open(_request(sha))
        file = env.pr_dir / pr.id
        file.write_text(
            file.read_text(encoding="utf-8").replace("status: open", "status: rejected"),
            encoding="utf-8",
        )
        assert await env.port.outcome(pr) == PrOutcome("rejected", None, None)
        assert await env.port.find_open("code") is None

    async def test_missing_file_is_rejected(self, env: Env) -> None:
        pr = _tracked(f"{RUN_ID}-code.md", "sync/code-1")
        assert await env.port.outcome(pr) == PrOutcome("rejected", None, None)

    async def test_status_merged_after_branch_deleted(self, env: Env) -> None:
        sha = await _make_branch(env, "sync/code-1")
        pr = await env.port.open(_request(sha))
        file = env.pr_dir / pr.id
        file.write_text(
            file.read_text(encoding="utf-8").replace("status: open", "status: merged"),
            encoding="utf-8",
        )
        await env.seed.run(["push", "-q", "origin", "--delete", "sync/code-1"])
        # 브랜치를 지웠고 final_head_sha도 없으면 봇이 올린 커밋을 최종으로 본다.
        assert await env.port.outcome(pr) == PrOutcome("merged", sha, None)

    async def test_merged_with_recorded_final_head_uses_no_git(self, tmp_dir: Path) -> None:
        port, directory, script = _fake_local(tmp_dir)
        final = "c" * 40
        (directory / f"{RUN_ID}-code.md").write_text(
            _pr_text(status="merged", final_head_sha=final), encoding="utf-8"
        )
        pr = _tracked(f"{RUN_ID}-code.md", "sync/code-1")
        assert await port.outcome(pr) == PrOutcome("merged", final, None)
        assert fake_git_calls(script) == []

    async def test_merged_with_bad_final_head_falls_back_to_branch_head(self, env: Env) -> None:
        sha = await _make_branch(env, "sync/code-1")
        pr = await env.port.open(_request(sha))
        fixed = await commit_files(env.seed, {"wiki/a.md": "고침\n"}, "fix")
        await env.seed.run(["push", "-q", "origin", "main:sync/code-1", "--force"])
        file = env.pr_dir / pr.id
        file.write_text(
            _pr_text(status="merged", final_head_sha="zzz", bot_head_sha=sha), encoding="utf-8"
        )
        assert await env.port.outcome(pr) == PrOutcome("merged", fixed, None)

    async def test_merged_with_bad_final_head_falls_back_to_bot_sha(self, env: Env) -> None:
        pr = _tracked(f"{RUN_ID}-code.md", "sync/never-pushed")
        (env.pr_dir).mkdir()
        (env.pr_dir / pr.id).write_text(
            _pr_text(status="merged", final_head_sha="zzz", branch="sync/never-pushed"),
            encoding="utf-8",
        )
        assert await env.port.outcome(pr) == PrOutcome("merged", SHA_A, None)

    async def test_find_open_returns_first_open_by_name(self, env: Env) -> None:
        sha = await _make_branch(env, "sync/code-1")
        await env.port.open(_request(sha, run_id="20261007-0900"))
        await env.port.open(_request(sha, run_id="20261006-0900"))
        assert await env.port.find_open("code") == "20261006-0900-code.md"

    async def test_open_branch_name_equal_to_base_is_kept_open(self, tmp_dir: Path) -> None:
        port, directory, script = _fake_local(tmp_dir)
        (directory / f"{RUN_ID}-code.md").write_text(_pr_text(branch="main"), encoding="utf-8")
        assert await port.find_open("code") == f"{RUN_ID}-code.md"
        assert fake_git_calls(script) == []
        assert await port.outcome(_tracked(f"{RUN_ID}-code.md", "main")) == OPEN
        assert fake_git_calls(script) == []

    async def test_fetch_final_head_ignores_deleted_branch(self, env: Env) -> None:
        sha = await _make_branch(env, "sync/code-1")
        pr = await env.port.open(_request(sha))
        await env.port.fetch_final_head(pr)  # 있는 브랜치
        await env.seed.run(["push", "-q", "origin", "--delete", "sync/code-1"])
        await env.port.fetch_final_head(pr)  # 지운 브랜치: GitError를 삼킨다


# ---------------------------------------------------------------- 로컬 포트 G20 (망가진 파일)


class TestLocalPortHostileFiles:
    async def test_broken_yaml_is_open(self, env: Env) -> None:
        sha = await _make_branch(env, "sync/code-1")
        pr = await env.port.open(_request(sha))
        file = env.pr_dir / pr.id
        file.write_text(
            file.read_text(encoding="utf-8").replace("status: open", "status: [open"),
            encoding="utf-8",
        )
        assert await env.port.find_open("code") == pr.id
        assert await env.port.outcome(pr) == OPEN

    async def test_broken_yaml_without_git(self, tmp_dir: Path) -> None:
        port, directory, script = _fake_local(tmp_dir)
        name = f"{RUN_ID}-ingest.md"
        (directory / name).write_text("---\nstatus: [open\n---\n본문", encoding="utf-8")
        assert await port.find_open("ingest") == name
        assert await port.find_open("code") is None
        assert await port.outcome(_tracked(name, kind="ingest")) == OPEN
        assert fake_git_calls(script) == []

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "본문뿐",
            "---\n---\n",
            "---\n- a\n- b\n---\n",
            "---\nstatus: open\n",  # 닫는 줄 없음
            "---\n!!python/object/apply:os.system ['id']\n---\n",
            "---\nstatus: &a open\nx: *a\n---\n",
            "---\na: !!timestamp 2026-10-07\n---\n",
            "\x00\x01\x02",
            "---\n" + "a: b\n" * 20_000 + "---\n",  # 64KB 초과
        ],
    )
    async def test_unparsable_files_are_open(self, tmp_dir: Path, text: str) -> None:
        port, directory, script = _fake_local(tmp_dir)
        name = f"{RUN_ID}-code.md"
        (directory / name).write_text(text, encoding="utf-8")
        assert await port.find_open("code") == name
        assert await port.outcome(_tracked(name)) == OPEN
        assert fake_git_calls(script) == []

    async def test_invalid_utf8_file_does_not_raise(self, tmp_dir: Path) -> None:
        port, directory, script = _fake_local(tmp_dir)
        name = f"{RUN_ID}-code.md"
        (directory / name).write_bytes(b"---\ntitle: \xff\xfe\n---\n\xc3")
        assert await port.find_open("code") == name
        assert fake_git_calls(script) == []

    @pytest.mark.parametrize(
        "status",
        ["merge", "Open", "OPEN", " open", "closed", "", "merged\n", "rejected ", "1", "null"],
    )
    async def test_odd_status_strings_are_open(self, tmp_dir: Path, env: Env, status: str) -> None:
        env.pr_dir.mkdir()
        name = f"{RUN_ID}-code.md"
        (env.pr_dir / name).write_text(_pr_text(status=status), encoding="utf-8")
        assert await env.port.outcome(_tracked(name, "sync/code-1")) == OPEN
        assert await env.port.find_open("code") == name

    async def test_status_typo_is_open(self, tmp_dir: Path) -> None:
        port, directory, script = _fake_local(tmp_dir)
        name = f"{RUN_ID}-code.md"
        (directory / name).write_text(_pr_text(status="merge"), encoding="utf-8")
        assert await port.find_open("code") == name
        assert await port.outcome(_tracked(name, "sync/code-1")) == OPEN
        assert fake_git_calls(script) == []

    @pytest.mark.parametrize(
        "status_yaml",
        ["[open]", "[rejected]", "{a: b}", "[]", "{}", "5", "5.5", "true", "null", "~", "[[x]]"],
    )
    async def test_odd_status_types_are_open(self, tmp_dir: Path, status_yaml: str) -> None:
        port, directory, script = _fake_local(tmp_dir)
        name = f"{RUN_ID}-code.md"
        text = _pr_text().replace("status: open", f"status: {status_yaml}")
        (directory / name).write_text(text, encoding="utf-8")
        assert await port.find_open("code") == name
        assert await port.outcome(_tracked(name, "sync/code-1")) == OPEN
        assert fake_git_calls(script) == []

    async def test_status_missing_is_open(self, tmp_dir: Path) -> None:
        port, directory, script = _fake_local(tmp_dir)
        name = f"{RUN_ID}-code.md"
        text = _pr_text().replace("status: open\n", "")
        (directory / name).write_text(text, encoding="utf-8")
        assert await port.find_open("code") == name
        assert await port.outcome(_tracked(name, "sync/code-1")) == OPEN
        assert fake_git_calls(script) == []

    @pytest.mark.parametrize(
        "field",
        [
            'branch: "--upload-pack=x"',
            'branch: "../x"',
            'branch: ""',
            "branch: [a]",
            "branch: 5",
            "branch: null",
            "branch: {a: b}",
            'branch: "a\\nb"',
            'branch: "a\\u0000b"',
            "branch: refs/heads/main",
            "branch: HEAD",
            f'branch: "{"a" * 5000}"',
            'bot_head_sha: "--upload-pack=x"',
            "bot_head_sha: zzz",
            "bot_head_sha: [x]",
            "bot_head_sha: 5",
            f'bot_head_sha: "{SHA_A}\\n"',
            f'bot_head_sha: "{SHA_A.upper()}"',
        ],
    )
    async def test_unsafe_fields_never_reach_git(self, tmp_dir: Path, field: str) -> None:
        port, directory, script = _fake_local(tmp_dir)
        name = f"{RUN_ID}-code.md"
        key = field.split(":", 1)[0]
        lines = [ln for ln in _pr_text().split("\n") if not ln.startswith(f"{key}:")]
        text = "\n".join([lines[0], field, *lines[1:]])
        (directory / name).write_text(text, encoding="utf-8")
        assert await port.find_open("code") == name
        assert fake_git_calls(script) == []

    async def test_unsafe_status_merged_and_rejected_with_bad_branch_stay_open(
        self, tmp_dir: Path
    ) -> None:
        port, directory, script = _fake_local(tmp_dir)
        for run_id, status in (("20261007-0900", "merged"), ("20261007-0901", "rejected")):
            text = _pr_text(status=status, branch="--upload-pack=x")
            (directory / f"{run_id}-code.md").write_text(text, encoding="utf-8")
        assert await port.find_open("code") == "20261007-0900-code.md"
        assert fake_git_calls(script) == []

    async def test_unrelated_files_and_links_are_ignored(self, tmp_dir: Path) -> None:
        port, directory, script = _fake_local(tmp_dir)
        valid = _pr_text()
        outside = tmp_dir / "outside.md"
        outside.write_text(valid, encoding="utf-8")
        for name in (
            "notes.md",
            ".20261007-0900-code.md.123.tmp",
            "20261007-0900-code.md\n",
            "20261007-0900-code.md.bak",
            "x20261007-0900-code.md",
            "20261007-0900-other.md",
            "２０２６１００７-0900-code.md",
        ):
            (directory / name).write_text(valid, encoding="utf-8")
        (directory / "20261007-0901-code.md").symlink_to(outside)
        (directory / "20261007-0902-code.md").symlink_to(tmp_dir / "없는파일")
        (directory / "20261007-0903-ingest.md").symlink_to(directory)  # 폴더 링크
        (directory / "20261007-0904-code.md").mkdir()  # 폴더
        fifo = directory / "20261007-0905-code.md"
        os.mkfifo(fifo)
        assert await port.find_open("code") is None
        assert await port.find_open("ingest") is None
        assert fake_git_calls(script) == []

    async def test_regular_file_among_junk_is_found(self, tmp_dir: Path) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        (directory / "notes.md").write_text("x", encoding="utf-8")
        (directory / "20261007-0901-code.md").symlink_to(tmp_dir / "x")
        (directory / "20261007-0910-code.md").write_text("깨짐", encoding="utf-8")
        assert await port.find_open("code") == "20261007-0910-code.md"

    async def test_missing_or_non_directory_folder(self, tmp_dir: Path) -> None:
        git, script = _fake_git(tmp_dir)
        missing = LocalPrPort(directory=tmp_dir / "none", data_git=git, base="main", now=_now)
        assert await missing.find_open("code") is None
        file = tmp_dir / "file"
        file.write_text("x", encoding="utf-8")
        not_dir = LocalPrPort(directory=file, data_git=git, base="main", now=_now)
        assert await not_dir.find_open("code") is None
        assert await not_dir.outcome(_tracked(f"{RUN_ID}-code.md")) == PrOutcome(
            "rejected", None, None
        )
        assert fake_git_calls(script) == []

    async def test_oversized_file_is_open_whatever_it_says(self, tmp_dir: Path) -> None:
        port, directory, script = _fake_local(tmp_dir)
        name = f"{RUN_ID}-code.md"
        text = _pr_text(status="rejected") + "x" * (2 * 1024 * 1024)
        assert len(text.encode("utf-8")) > 2_000_000
        (directory / name).write_text(text, encoding="utf-8")
        assert await port.find_open("code") == name
        assert await port.outcome(_tracked(name)) == OPEN
        assert fake_git_calls(script) == []

    async def test_size_limit_boundary(self, tmp_dir: Path) -> None:
        port, directory, script = _fake_local(tmp_dir)
        head = _pr_text(status="rejected")
        exact = head.encode("utf-8") + b"x" * (MAX_LOCAL_PR_BYTES - len(head.encode("utf-8")))
        assert len(exact) == MAX_LOCAL_PR_BYTES
        (directory / f"{RUN_ID}-code.md").write_bytes(exact)
        over = exact + b"x"
        (directory / f"{RUN_ID}-ingest.md").write_bytes(over)
        assert await port.outcome(_tracked(f"{RUN_ID}-code.md")) == PrOutcome(
            "rejected", None, None
        )
        assert await port.outcome(_tracked(f"{RUN_ID}-ingest.md", kind="ingest")) == OPEN
        assert fake_git_calls(script) == []

    async def test_tracked_file_replaced_by_non_regular_is_open_not_hang(
        self, tmp_dir: Path
    ) -> None:
        port, directory, script = _fake_local(tmp_dir)
        outside = tmp_dir / "outside.md"
        outside.write_text(_pr_text(status="rejected"), encoding="utf-8")
        (directory / "20261007-0900-code.md").symlink_to(outside)
        (directory / "20261007-0901-code.md").mkdir()
        os.mkfifo(directory / "20261007-0902-code.md")
        for name in ("20261007-0900-code.md", "20261007-0901-code.md", "20261007-0902-code.md"):
            assert await port.outcome(_tracked(name)) == OPEN
        assert fake_git_calls(script) == []

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../x.md",
            "x.md",
            f"{RUN_ID}-code.md\n",
            "/etc/passwd",
            "",
            f"{RUN_ID}-code.md/..",
            f"sub/{RUN_ID}-code.md",
            f"{RUN_ID}-other.md",
            "7",
        ],
    )
    async def test_outcome_rejects_ids_that_are_not_pr_file_names(
        self, tmp_dir: Path, bad_id: str
    ) -> None:
        port, directory, script = _fake_local(tmp_dir)
        (tmp_dir / "x.md").write_text(_pr_text(status="rejected"), encoding="utf-8")
        with pytest.raises(ValueError, match="PR id"):
            await port.outcome(_raw_tracked(bad_id))
        with pytest.raises(ValueError, match="PR id"):
            await port.fetch_final_head(_raw_tracked(bad_id))
        assert fake_git_calls(script) == []
        assert list(directory.iterdir()) == []

    async def test_fetch_final_head_with_unsafe_branch_is_blocked(self, tmp_dir: Path) -> None:
        port, _, script = _fake_local(tmp_dir)
        unsafe = TrackedPr.model_construct(
            id=f"{RUN_ID}-code.md", kind="code", branch="--upload-pack=x", bot_head_sha=SHA_A
        )
        with pytest.raises(ValueError):
            await port.fetch_final_head(unsafe)
        assert fake_git_calls(script) == []

    async def test_outcome_with_unsafe_tracked_branch_is_blocked(self, tmp_dir: Path) -> None:
        port, directory, script = _fake_local(tmp_dir)
        name = f"{RUN_ID}-code.md"
        (directory / name).write_text(_pr_text(), encoding="utf-8")
        unsafe = TrackedPr.model_construct(id=name, kind="code", branch="../x", bot_head_sha=SHA_A)
        assert await port.outcome(unsafe) == OPEN
        assert fake_git_calls(script) == []


# ---------------------------------------------------------------- 로컬 포트 open 입력과 직렬화


HOSTILE_TEXTS = [
    "",
    "평범한 제목",
    '"따옴표"',
    "'작은따옴표'",
    "a\n---\nb",
    "---",
    "--- ",
    "...",
    "line1\nline2",
    "line1\r\nline2",
    "x: y",
    "key: value\nstatus: rejected",
    "- item",
    "- a\n- b",
    "!!python/object/apply:os.system ['id']",
    "!!python/name:os.system",
    "&anchor [1, 2]",
    "*alias",
    "<<: {a: 1}",
    "#comment",
    "\x00nul",
    "tab\there",
    "\u2028\u2029",
    "a\u0086b",
    "\ufeff앞의 BOM",
    "yes",
    "no",
    "123",
    "0x1F",
    "1:30",
    "null",
    "~",
    "true",
    "2026-10-07",
    "|",
    ">",
    "@everyone",
    "%YAML 1.1",
    "? key",
    "{a: b}",
    "[a, b]",
    "\x1b[31m색\x1b[0m",
    "😀 이모지",
    "\u202e뒤집힘",
    " 앞뒤 공백 ",
    "trailing colon:",
    "'---'\n---\n",
]


class TestLocalPortOpen:
    @pytest.mark.parametrize("title", HOSTILE_TEXTS)
    async def test_hostile_title_roundtrips(self, tmp_dir: Path, title: str) -> None:
        port, directory, script = _fake_local(tmp_dir)
        pr = await port.open(_request(SHA_A, title=title))
        text = (directory / pr.id).read_bytes().decode("utf-8")
        parsed = parse_frontmatter(text)
        assert parsed.error is None
        assert parsed.data is not None
        assert parsed.data["title"] == title
        assert set(parsed.data) == {
            "title",
            "kind",
            "branch",
            "base",
            "status",
            "bot_head_sha",
            "lint_failed",
            "created_at",
        }
        assert parsed.data["status"] == "open"
        assert parsed.data["branch"] == "sync/code-1"

    @pytest.mark.parametrize("body", HOSTILE_TEXTS)
    async def test_hostile_body_roundtrips(self, tmp_dir: Path, body: str) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        pr = await port.open(_request(SHA_A, body=body))
        parsed = parse_frontmatter((directory / pr.id).read_bytes().decode("utf-8"))
        assert parsed.error is None
        assert parsed.data is not None
        assert parsed.data["status"] == "open"
        assert parsed.body == f"\n{body}\n"

    async def test_hostile_title_file_is_still_a_normal_open_pr(self, env: Env) -> None:
        sha = await _make_branch(env, "sync/code-1")
        title = "x\nstatus: rejected\n---\n!!python/object:os.system &a"
        pr = await env.port.open(_request(sha, title=title, body="---\nstatus: merged\n---\n"))
        assert await env.port.find_open("code") == pr.id
        assert await env.port.outcome(pr) == OPEN

    async def test_body_with_frontmatter_lookalike_cannot_change_status(
        self, tmp_dir: Path
    ) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        body = "---\nstatus: rejected\n---\n더 있음"
        pr = await port.open(_request(SHA_A, body=body))
        parsed = parse_frontmatter((directory / pr.id).read_text(encoding="utf-8"))
        assert parsed.data is not None
        assert parsed.data["status"] == "open"

    async def test_nel_in_title_is_replaced_but_kept_in_body(self, tmp_dir: Path) -> None:
        # PyYAML은 따옴표 안의 U+0085를 줄바꿈으로 읽어 값이 바뀐다 -> 제목에서만 공백으로 바꾼다.
        port, directory, _ = _fake_local(tmp_dir)
        pr = await port.open(_request(SHA_A, title="x\x85\x85y", body="c\x85d"))
        parsed = parse_frontmatter((directory / pr.id).read_bytes().decode("utf-8"))
        assert parsed.data is not None
        assert parsed.data["title"] == "x  y"
        assert parsed.body == "\nc\x85d\n"

    async def test_lone_surrogates_are_replaced(self, tmp_dir: Path) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        pr = await port.open(_request(SHA_A, title="a\udc80b", body="x\ud800y"))
        parsed = parse_frontmatter((directory / pr.id).read_text(encoding="utf-8"))
        assert parsed.data is not None
        assert parsed.data["title"] == "a�b"
        assert parsed.body == "\nx�y\n"

    async def test_huge_title_and_body_are_capped(self, tmp_dir: Path) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        pr = await port.open(_request(SHA_A, title="가" * 100_000, body="나" * 5_000_000))
        file = directory / pr.id
        assert file.stat().st_size < MAX_LOCAL_PR_BYTES
        parsed = parse_frontmatter(file.read_text(encoding="utf-8"))
        assert parsed.error is None
        assert parsed.data is not None
        assert isinstance(parsed.data["title"], str)
        assert 0 < len(parsed.data["title"]) <= 300
        assert parsed.data["status"] == "open"

    async def test_lint_failed_flag(self, tmp_dir: Path) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        pr = await port.open(_request(SHA_A, lint_failed=True, kind="ingest"))
        assert pr.id == f"{RUN_ID}-ingest.md"
        parsed = parse_frontmatter((directory / pr.id).read_text(encoding="utf-8"))
        assert parsed.data is not None
        assert parsed.data["lint_failed"] is True
        assert parsed.data["kind"] == "ingest"

    @pytest.mark.parametrize(
        "extra",
        [
            {"run_id": "../20261007-0900"},
            {"run_id": "20261007-0900\n"},
            {"run_id": "2026-10-07"},
            {"run_id": "２０２６１００７-0900"},
            {"run_id": ""},
            {"kind": "other"},
            {"branch": "--upload-pack=x"},
            {"branch": "../x"},
            {"base": "--x"},
            {"base": "a b"},
            {"bot_head_sha": "zzz"},
            {"bot_head_sha": SHA_A + "\n"},
        ],
    )
    async def test_invalid_request_is_refused_before_writing(
        self, tmp_dir: Path, extra: dict[str, Any]
    ) -> None:
        port, directory, script = _fake_local(tmp_dir)
        with pytest.raises(ValueError) as info:
            await port.open(_request(SHA_A, **extra))
        for value in extra.values():
            if isinstance(value, str) and len(value) > 3:
                assert value not in str(info.value)
        assert list(directory.iterdir()) == []
        assert fake_git_calls(script) == []

    def test_constructor_validates_base(self, tmp_dir: Path) -> None:
        git, _ = _fake_git(tmp_dir)
        for bad in ("--x", "../main", "", "a b"):
            with pytest.raises(ValueError):
                LocalPrPort(directory=tmp_dir, data_git=git, base=bad, now=_now)

    async def test_open_creates_missing_directory(self, tmp_dir: Path) -> None:
        git, _ = _fake_git(tmp_dir)
        directory = tmp_dir / "state" / "local-prs"
        port = LocalPrPort(directory=directory, data_git=git, base="main", now=_now)
        pr = await port.open(_request(SHA_A))
        assert (directory / pr.id).is_file()

    async def test_refuses_to_overwrite_existing_pr_file(self, tmp_dir: Path) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        pr = await port.open(_request(SHA_A, body="처음"))
        before = (directory / pr.id).read_bytes()
        with pytest.raises(ValueError, match="이미") as info:
            await port.open(_request(SHA_A, body="둘째", title="비밀제목"))
        assert "비밀제목" not in str(info.value)
        assert (directory / pr.id).read_bytes() == before
        assert [p.name for p in directory.iterdir()] == [pr.id]

    async def test_refuses_to_overwrite_a_directory_or_fifo(self, tmp_dir: Path) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        (directory / f"{RUN_ID}-code.md").mkdir()
        os.mkfifo(directory / f"{RUN_ID}-ingest.md")
        for kind in ("code", "ingest"):
            with pytest.raises(ValueError, match="이미"):
                await port.open(_request(SHA_A, kind=kind))

    async def test_symlink_at_the_name_is_replaced_not_followed(self, tmp_dir: Path) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        outside = tmp_dir / "outside.md"
        outside.write_text("원본", encoding="utf-8")
        (directory / f"{RUN_ID}-code.md").symlink_to(outside)
        pr = await port.open(_request(SHA_A))
        assert not (directory / pr.id).is_symlink()
        assert outside.read_text(encoding="utf-8") == "원본"
        assert parse_frontmatter((directory / pr.id).read_text(encoding="utf-8")).data is not None

    @pytest.mark.parametrize("value", ["yes", 1, 0, None, "", "false"])
    async def test_lint_failed_must_be_bool(self, tmp_dir: Path, value: Any) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        with pytest.raises(ValueError):
            await port.open(_request(SHA_A, lint_failed=value))
        assert list(directory.iterdir()) == []

    async def test_created_at_is_kst(self, tmp_dir: Path) -> None:
        port, directory, _ = _fake_local(tmp_dir)
        pr = await port.open(_request(SHA_A))
        parsed = parse_frontmatter((directory / pr.id).read_text(encoding="utf-8"))
        assert parsed.data is not None
        assert parsed.data["created_at"] == "2026-10-07T09:00:00+09:00"


# ---------------------------------------------------------------- GitHub 포트


class _FlakyLabels(FakeGitHub):
    """`add_labels`가 처음 `failures`번 실패하는 가짜."""

    def __init__(self, *, failures: int, error: BaseException | None = None) -> None:
        super().__init__()
        self._failures = failures
        self._error = error or GitHubError("라벨 실패", 502)
        self.add_attempts = 0

    async def add_labels(self, repo: RepoRef, number: int, labels: Sequence[str]) -> None:
        self.add_attempts += 1
        if self.add_attempts <= self._failures:
            raise self._error
        await super().add_labels(repo, number, labels)


class TestGitHubPort:
    def _port(self, gh: FakeGitHub, root: Path | None = None) -> tuple[GitHubPrPort, Path | None]:
        script: Path | None = None
        if root is None:
            git = GitClient(Path("/none"))
        else:
            git, script = _fake_git(root)
        return GitHubPrPort(github=gh, repo=REPO, data_git=git), script

    def test_is_a_pr_port(self) -> None:
        port: PrPort = GitHubPrPort(github=FakeGitHub(), repo=REPO, data_git=GitClient(Path("/x")))
        assert port is not None

    async def test_find_open_by_label(self) -> None:
        gh = FakeGitHub(
            open=[
                pull_summary(5, state="open", labels=("wiki-sync:ingest",)),
                pull_summary(6, state="open", labels=("wiki-sync:code",)),
                pull_summary(7, state="open", labels=()),
            ]
        )
        port, _ = self._port(gh)
        assert await port.find_open("code") == "6"
        assert await port.find_open("ingest") == "5"

    async def test_find_open_by_branch_when_label_is_missing(self) -> None:
        gh = FakeGitHub(
            open=[
                pull_summary(4, state="open", head_ref="sync/code-1"),  # 이름 형식이 아니다
                pull_summary(5, state="open", head_ref="sync/ingest-20261007-0900"),
                pull_summary(6, state="open", head_ref="sync/code-20261007-0900"),
                pull_summary(7, state="open", head_ref="sync/code-20261007-0900\n"),
                pull_summary(8, state="open", head_ref="x/sync/code-20261007-0900"),
            ]
        )
        port, _ = self._port(gh)
        assert await port.find_open("code") == "6"
        assert await port.find_open("ingest") == "5"

    async def test_find_open_none(self) -> None:
        port, _ = self._port(FakeGitHub(open=[pull_summary(7, state="open")]))
        assert await port.find_open("code") is None

    async def test_open_creates_and_labels(self) -> None:
        gh = FakeGitHub()
        port, _ = self._port(gh)
        pr = await port.open(_request("c" * 40, lint_failed=True))
        assert pr == TrackedPr(id="101", kind="code", branch="sync/code-1", bot_head_sha="c" * 40)
        assert gh.created == [
            {
                "title": "위키 코드 반영",
                "head": "sync/code-1",
                "base": "main",
                "body": "PR 본문입니다",
            }
        ]
        assert gh.ensured == ["wiki-sync:code", "점검 실패"]
        assert gh.added == [(101, ("wiki-sync:code", "점검 실패"))]
        assert [c[0] for c in gh.calls] == [
            "ensure_label",
            "ensure_label",
            "create_pull",
            "add_labels",
        ]

    async def test_open_without_lint_failed_has_one_label(self) -> None:
        gh = FakeGitHub()
        port, _ = self._port(gh)
        await port.open(_request("c" * 40, kind="ingest"))
        assert gh.ensured == ["wiki-sync:ingest"]
        assert gh.added == [(101, ("wiki-sync:ingest",))]
        assert gh.ensured_colors == {"wiki-sync:ingest": "0e8a16"}

    @pytest.mark.parametrize(
        "extra",
        [
            {"kind": "other"},
            {"branch": "--upload-pack=x"},
            {"branch": "../x"},
            {"base": "a b"},
            {"bot_head_sha": "zzz"},
            {"bot_head_sha": SHA_A + "\n"},
        ],
    )
    async def test_invalid_request_makes_no_github_call(self, extra: dict[str, Any]) -> None:
        gh = FakeGitHub()
        port, _ = self._port(gh)
        with pytest.raises(ValueError):
            await port.open(_request(SHA_A, **extra))
        assert gh.calls == []

    async def test_lone_surrogates_are_replaced(self) -> None:
        gh = FakeGitHub()
        port, _ = self._port(gh)
        await port.open(_request(SHA_A, title="a\udc80", body="b\ud800"))
        assert gh.created[0]["title"] == "a�"
        assert gh.created[0]["body"] == "b�"

    async def test_add_labels_retries_once(self) -> None:
        gh = _FlakyLabels(failures=1)
        port, _ = self._port(gh)
        pr = await port.open(_request(SHA_A, branch="sync/code-20261007-0900"))
        assert pr.id == "101"
        assert gh.add_attempts == 2
        assert gh.added == [(101, ("wiki-sync:code",))]
        assert port.warnings == []

    async def test_add_labels_failure_still_returns_the_pr(self) -> None:
        secret = "ghp_" + "s" * 36
        gh = _FlakyLabels(failures=99, error=GitHubError(f"실패 {secret}", 500))
        port, _ = self._port(gh)
        branch = "sync/code-20261007-0900"
        pr = await port.open(_request(SHA_A, branch=branch))
        assert pr == TrackedPr(id="101", kind="code", branch=branch, bot_head_sha=SHA_A)
        assert gh.add_attempts == 2  # 한 번만 다시 시도한다
        assert len(port.warnings) == 1
        assert "101" in port.warnings[0]
        assert secret not in port.warnings[0]
        assert "GitHubError" in port.warnings[0]
        # 라벨이 없는 PR이어도 다음 실행이 브랜치 이름으로 찾아 중복 PR을 만들지 않는다.
        gh.open = [pull_summary(101, state="open", head_ref=branch, labels=())]
        assert await port.find_open("code") == "101"

    async def test_add_labels_cancellation_is_not_swallowed(self) -> None:
        gh = _FlakyLabels(failures=99, error=asyncio.CancelledError())
        port, _ = self._port(gh)
        with pytest.raises(asyncio.CancelledError):
            await port.open(_request(SHA_A))

    async def test_create_pull_failure_still_raises(self) -> None:
        gh = FakeGitHub(fail=GitHubError("실패", 500))
        port, _ = self._port(gh)
        with pytest.raises(GitHubError):
            await port.open(_request(SHA_A))
        assert port.warnings == []

    @pytest.mark.parametrize("value", ["yes", 1, None, ""])
    async def test_lint_failed_must_be_bool(self, value: Any) -> None:
        gh = FakeGitHub()
        port, _ = self._port(gh)
        with pytest.raises(ValueError):
            await port.open(_request(SHA_A, lint_failed=value))
        assert gh.calls == []

    @pytest.mark.parametrize("head", ["zzz", "", "A" * 40, "f" * 40 + "\n"])
    async def test_outcome_gates_merged_head_sha(self, head: str) -> None:
        gh = FakeGitHub(details={1: pull_detail(1, state="closed", merged=True, head_sha=head)})
        port, _ = self._port(gh)
        got = await port.outcome(_tracked("1"))
        assert got == PrOutcome("merged", SHA_A, "2026-10-07T00:00:00Z")

    async def test_open_propagates_github_errors(self) -> None:
        gh = FakeGitHub(fail=GitHubError("실패", 500))
        port, _ = self._port(gh)
        with pytest.raises(GitHubError):
            await port.open(_request(SHA_A))

    async def test_outcome_from_details(self) -> None:
        gh = FakeGitHub(
            details={
                1: pull_detail(1, state="open", merged=False, closed_at=None, head_sha="e" * 40),
                2: pull_detail(
                    2,
                    state="closed",
                    merged=True,
                    closed_at="2026-10-08T00:00:00Z",
                    head_sha="e" * 40,
                ),
                3: pull_detail(
                    3,
                    state="closed",
                    merged=False,
                    closed_at="2026-10-08T00:00:00Z",
                    head_sha="e" * 40,
                ),
            }
        )
        port, _ = self._port(gh)
        assert await port.outcome(_tracked("1")) == OPEN
        assert await port.outcome(_tracked("2")) == PrOutcome(
            "merged", "e" * 40, "2026-10-08T00:00:00Z"
        )
        assert await port.outcome(_tracked("3")) == PrOutcome(
            "rejected", None, "2026-10-08T00:00:00Z"
        )

    async def test_outcome_with_unknown_pr_raises_github_error(self) -> None:
        port, _ = self._port(FakeGitHub())
        with pytest.raises(GitHubError):
            await port.outcome(_tracked("9"))

    @pytest.mark.parametrize(
        "bad_id",
        ["7; rm", "", "x", "7\n", "-7", "7 ", "٣", "７", "1" * 40, "7/../8", "7.0", "0x7"],
    )
    async def test_invalid_id_raises_value_error(self, tmp_dir: Path, bad_id: str) -> None:
        gh = FakeGitHub()
        port, script = self._port(gh, tmp_dir)
        pr = _raw_tracked(bad_id)
        with pytest.raises(ValueError, match="PR id"):
            await port.outcome(pr)
        with pytest.raises(ValueError, match="PR id"):
            await port.fetch_final_head(pr)
        assert gh.calls == []
        assert script is not None
        assert fake_git_calls(script) == []

    async def test_fetch_final_head_uses_pull_ref(self, tmp_dir: Path) -> None:
        port, script = self._port(FakeGitHub(), tmp_dir)
        with pytest.raises(GitError):  # 가짜 git은 fetch를 모른다 — 호출 기록만 본다
            await port.fetch_final_head(_tracked("7"))
        assert script is not None
        [call] = fake_git_calls(script)
        assert call[2:] == [
            "fetch",
            "-q",
            "--no-tags",
            "origin",
            "+refs/pull/7/head:refs/remotes/origin/pr-7",
        ]
        assert call[0].startswith("--git-dir=") and call[1].startswith("--work-tree=")

    async def test_fetch_final_head_needs_a_repository(self, tmp_dir: Path) -> None:
        git, script = _fake_git(tmp_dir, repo=False)
        port = GitHubPrPort(github=FakeGitHub(), repo=REPO, data_git=git)
        with pytest.raises(CloneStateError):
            await port.fetch_final_head(_tracked("7"))
        assert fake_git_calls(script) == []

    async def test_fetch_final_head_with_real_git_and_pull_ref(self, tmp_dir: Path) -> None:
        bare = tmp_dir / "data.git"
        seed = await seed_bare(bare, tmp_dir / "seed", {"raw/a.md": "a"})
        sha = await commit_files(seed, {"raw/b.md": "b"}, "pr head")
        await seed.run(["update-ref", "refs/pull/7/head", sha])
        await seed.run(["push", "-q", "origin", "refs/pull/7/head:refs/pull/7/head"])
        clone = tmp_dir / "clone"
        data_git = GitClient(clone, allowed_protocols="file", timeout_s=60.0)
        await ensure_clone(data_git, str(bare))
        port = GitHubPrPort(github=FakeGitHub(), repo=REPO, data_git=data_git)
        await port.fetch_final_head(_tracked("7"))
        got = (await data_git.run(["rev-parse", "refs/remotes/origin/pr-7"])).text.strip()
        assert got == sha
