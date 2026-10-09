"""동기화 단계 테스트 환경(Task 13).

임시 폴더 하나에 코드 저장소 `worker`와 데이터 저장소의 bare 저장소, 두 개의 작업 사본, `data-sync`
사본, 가짜 Claude, 기록하는 알림기를 만든다. 완전히 닫힌 환경이다.

- 네트워크·실제 Claude 없음. 모든 `GitClient`는 `allowed_protocols="file"`이고 설정의 모든 저장소
  URL은 로컬 절대 경로임을 확인한다(아니면 `AssertionError`).
- 사용자 git 설정·환경에 기대지 않는다: `TEAM_WIKI_HOME`을 `resolve_paths`에 직접 넘기고 git 자식
  프로세스의 `HOME`은 임시 폴더다(`GIT_CONFIG_GLOBAL=/dev/null`은 `git_env`가 고정한다).
- 시각은 결정적이다: 기본 `2026-10-07T00:00:00Z`, `set_now`로만 바뀐다.
- 데이터 저장소의 `wiki/` 아래에는 아무것도 두지 않는다(G9: `.gitkeep`이 쓰기 루트 검사를 흔들지
  않게). `raw/`에는 `.gitkeep`과 `raw_files`만 둔다.
"""

import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from team_wiki_engine.claude.types import RunClaudeInput
from team_wiki_engine.config.load import Paths, parse_config, resolve_paths
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.git.client import GitClient
from team_wiki_engine.git.ops import fetch_branch
from team_wiki_engine.metrics.record import STATE_FILES
from team_wiki_engine.sync.data_copy import prepare_data_copy
from team_wiki_engine.sync.lock import acquire_lock
from team_wiki_engine.sync.pr_port import LocalPrPort, PrPort
from team_wiki_engine.sync.stage import StageContext, SyncDeps
from tests.helpers import write_tree
from tests.helpers.git import seed_bare
from tests.helpers.notify import RecordingNotifier
from tests.helpers.runner import Script, ScriptedRunner

ENGINE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_NOW = datetime(2026, 10, 7, 0, 0, 0, tzinfo=UTC)
ZERO_SHA = "0" * 40

_STATUS_TAIL = "archived: false\nowners: []\n"
_RAW_NAME = re.compile(r"원본 파일: raw/(.+)$", re.MULTILINE)


def _front(fields: str = "") -> str:
    return f"---\n{fields}updated: 2026-10-07\n---\n\n"


def default_script(inp: RunClaudeInput, call: int) -> Mapping[str, Any]:
    """점검을 통과하는 최소 위키를 쓰는 가짜 Claude. `skills/*/SKILL.md`의 규칙을 흉내 낸다."""
    if inp.profile == "sync-code":
        write_tree(
            inp.cwd,
            {
                "wiki/index.md": f"{_front()}# 목차\n\n- [worker](status/worker.md)\n",
                "wiki/overview.md": f"{_front()}# 개요\n",
                "wiki/open-questions.md": (
                    f"{_front()}# 미결\n\n### [2026-10-07] 샘플 어긋남\n"
                    "- 종류: 어긋남\n- 내용: 테스트\n"
                ),
                "wiki/status/worker.md": (
                    _front(f'repo: worker\nbaseline_commit: "{ZERO_SHA}"\n{_STATUS_TAIL}')
                    + "# worker\n"
                ),
            },
        )
        return {"text": "반영했습니다.\n\n## 확신이 낮은 부분\n- 없음", "session_id": "sess-code"}
    if "재확인" in inp.prompt:
        return {"text": "재확인 완료", "session_id": "sess-recheck"}
    match = _RAW_NAME.search(inp.prompt)
    if match is None:
        raise ValueError("원본 파일 이름이 프롬프트에 없습니다")
    name = match.group(1)
    files = {
        f"wiki/sources/{name}": (
            _front(f"raw: {name}\ndate: {name[:10]}\naffects: []\n") + "# 요약\n"
        )
    }
    if not (inp.cwd / "wiki" / "index.md").exists():
        files["wiki/index.md"] = f"{_front()}# 목차\n"
    write_tree(inp.cwd, files)
    return {"text": f"## 문서 분석\n- 새로 정함: {name}", "session_id": "sess-ingest"}


class _Clock:
    """`deps.now`가 읽는 시계. `SyncEnv.set_now`로만 바뀐다."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _local_client(directory: Path, home: Path) -> GitClient:
    base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
    return GitClient(directory, allowed_protocols="file", timeout_s=60.0, base_env=base_env)


def _require_local_url(label: str, url: str) -> None:
    assert os.path.isabs(url) and "://" not in url and not url.startswith("-"), (
        f"{label}의 URL이 로컬 절대 경로가 아닙니다"
    )


@dataclass
class SyncEnv:
    root: Path
    paths: Paths
    config: TeamConfig
    deps: SyncDeps
    code_work: GitClient
    data_work: GitClient
    data_bare: Path
    runner: ScriptedRunner
    notifier: RecordingNotifier
    clock: _Clock = field(repr=False)

    def set_now(self, d: datetime) -> None:
        if d.tzinfo is None:
            raise ValueError("시각은 시간대가 있어야 합니다")
        self.clock.value = d

    async def merge_branch(self, branch: str) -> None:
        """작업 브랜치를 `main`에 `--no-ff`로 합쳐 올리고 `data-sync`의 `origin/main`을 갱신한다."""
        await self.data_work.run(["fetch", "-q", "origin"])
        await self.data_work.run(["merge", "-q", "--ff-only", "origin/main"])
        await self.data_work.run(
            ["merge", "-q", "--no-ff", "-m", f"merge {branch}", f"origin/{branch}"]
        )
        await self.data_work.run(["push", "-q", "origin", "main"])
        await fetch_branch(self.deps.data_git, "main")

    def stage(self, run_id: str) -> AbstractAsyncContextManager[StageContext]:
        """`sync.lock`을 잡은 단계 문맥. 나오면 잠금을 놓는다."""
        return self._stage(run_id)

    @asynccontextmanager
    async def _stage(self, run_id: str) -> AsyncIterator[StageContext]:
        now = self.clock()
        lock = acquire_lock(self.paths.state_dir / STATE_FILES.lock, now=now)
        if lock is None:
            raise AssertionError("sync.lock을 잡을 수 없습니다(다른 곳이 쥐고 있음)")
        try:
            yield StageContext(deps=self.deps, run_id=run_id, now=now, lock=lock)
        finally:
            lock.release()


async def make_sync_env(
    root: Path,
    *,
    raw_files: Mapping[str, str] | None = None,
    review_mode: Literal["pr", "direct"] = "pr",
    extra_repos: Sequence[Mapping[str, str]] = (),
) -> SyncEnv:
    """`root` 안에 동기화 테스트 환경을 만든다. `root`는 비어 있는 임시 폴더여야 한다."""
    home = root / "home"
    git_home = root / "git-home"
    git_home.mkdir(parents=True, exist_ok=True)
    paths = resolve_paths({"TEAM_WIKI_HOME": str(home)}, ENGINE_DIR)

    code_bare = root / "worker.git"
    code_work = await seed_bare(code_bare, root / "worker-work", {"src/A.kt": "class A"})
    data_bare = root / "data.git"
    data_work = await seed_bare(
        data_bare, root / "data-work", {"raw/.gitkeep": "", **(raw_files or {})}
    )

    repos = [{"name": "worker", "url": str(code_bare)}, *(dict(r) for r in extra_repos)]
    config = parse_config(
        {"repos": repos, "dataRepo": {"url": str(data_bare)}, "reviewMode": review_mode}
    )
    for repo in config.repos:
        _require_local_url(f"저장소 {repo.name}", repo.url)
    _require_local_url("데이터 저장소", config.data_repo.url)

    paths.repos_dir.mkdir(parents=True, exist_ok=True)
    data_git = _local_client(paths.data_sync_dir, git_home)
    await prepare_data_copy(data_git, config.data_repo.url, config.data_repo.branch)

    clock = _Clock(DEFAULT_NOW)
    runner = ScriptedRunner(default_script)
    notifier = RecordingNotifier()
    pr_port: PrPort | None = None
    if config.review_mode == "pr":
        pr_port = LocalPrPort(
            directory=paths.local_prs_dir,
            data_git=data_git,
            base=config.data_repo.branch,
            now=clock,
        )
    deps = SyncDeps(
        config=config,
        paths=paths,
        runner=runner,
        github=None,
        pr_port=pr_port,
        data_git=data_git,
        code_git=_local_client(paths.repos_dir, git_home),
        notifier=notifier,
        now=clock,
    )
    return SyncEnv(
        root=root,
        paths=paths,
        config=config,
        deps=deps,
        code_work=code_work,
        data_work=data_work,
        data_bare=data_bare,
        runner=runner,
        notifier=notifier,
        clock=clock,
    )


__all__ = ["DEFAULT_NOW", "Script", "SyncEnv", "default_script", "make_sync_env"]
