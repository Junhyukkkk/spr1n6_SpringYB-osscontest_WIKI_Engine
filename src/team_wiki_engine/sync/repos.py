"""코드 사본(clone) 동기화, 코드 사본 시점 기록, 봇 프롬프트용 기준 블록.

보안·안정성 규칙:
- 저장소 하나가 실패해도(`GitError`·`CloneStateError`·`ValueError`·`OSError`) 다음 저장소로
  넘어가고, 실패 메시지는 `safe_text`로 가려 300자로 자른다. URL의 사용자 정보는 `git/ops.py`가
  git을 부르기 전에 거부하고(메시지에 입력 값을 싣지 않는다) `GitClient`가 git 오류 메시지에서
  지우므로 이 모듈은 URL을 메시지에 싣지 않는다.
- 저장소 이름은 폴더 이름으로 쓰이므로 `[a-z0-9-]+`(설정 검증과 같은 규칙)가 아니면 폴더를 만들기
  전에 실패로 모은다(`repos_dir` 밖으로 나가는 길을 막는다).
- 끊긴 옛 clone(`.git`은 실제 폴더인데 `HEAD`가 없다)은 다시 받는다. 이때 `repos/<이름>`의
  **최상위 항목을 전부 지운다**(`clear_children`; 링크는 따라가지 않고 링크만 지운다). 코드 사본은
  서버가 만든 읽기 전용 사본이라 지워도 잃는 것이 없다. `.git`이 링크·파일이거나 `.git` 없이 채워진
  폴더는 지우지 않고 실패로 모은다. 새 clone은 조각 폴더에 받아 원자적으로 놓고, 취소 뒤 남은
  `.git/*.lock`(`index.lock` 등 일반 파일)은 한 번 지우고 다시 시도한다.
- 예전 clone은 `core.symlinks`가 켜진 채 링크가 풀려 있을 수 있으므로 `disable_symlinks`로 설정을
  바꾸고 링크를 지운 뒤 checkout이 일반 파일로 다시 만든다.
- 코드 사본 시점 파일은 사람(또는 다른 프로세스)이 망가뜨려도 `None`으로 읽힌다. `basis_lines`는
  파일에서 온 이름·SHA를 `clean()`으로 이스케이프하고 길이·개수를 제한한다(봇 프롬프트에 들어간다).
"""

import logging
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

from team_wiki_engine.config.schema import RepoConfig
from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import (
    CloneStateError,
    checkout_detached,
    disable_symlinks,
    ensure_clone,
    fetch_branch,
    remote_url,
    rev_parse,
    set_remote_url,
)
from team_wiki_engine.metrics.record import STATE_FILES, read_json, write_json
from team_wiki_engine.sync.data_copy import (
    clear_children,
    clone_fresh,
    is_fresh,
    remove_stale_git_locks,
    sweep_partials,
)
from team_wiki_engine.sync.stage import scrub_paths
from team_wiki_engine.util.aio import run_to_completion
from team_wiki_engine.util.time import kst_iso, kst_parts, parse_instant
from team_wiki_engine.wiki.baseline import Baseline
from team_wiki_engine.wiki.lint_secrets import clean, safe_text

NO_RECORD = "기록 없음"
NONE_LABEL = "없음"
_log = logging.getLogger(__name__)
FAILURE_CHARS = 300
MAX_BASIS_REPOS = 100
MAX_BASIS_NAME_CHARS = 100
SHORT_SHA = 7

_REPO_NAME = re.compile(r"[a-z0-9-]{1,100}")


@dataclass(frozen=True)
class CodeSyncResult:
    heads: dict[str, str]
    failures: dict[str, str]


class CodeSnapshot(BaseModel):
    """코드 사본 시점. 계획 3의 봇이 "코드 사본 시점"으로 읽는다(상태 파일이라 snake_case)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    fetched_at: str
    repos: dict[str, str]


_SNAPSHOT_ADAPTER: TypeAdapter[CodeSnapshot | None] = TypeAdapter(CodeSnapshot | None)


# ---------------------------------------------------------------- 코드 사본


def _is_half_clone(directory: Path) -> bool:
    """`.git`은 실제 폴더인데 `HEAD`가 없는 폴더 — 끊긴 옛 clone이 남긴 것."""
    try:
        if not stat.S_ISDIR(os.lstat(directory / ".git").st_mode):
            return False  # 링크·파일은 반쪽 clone이 아니라 거부 대상이다(CloneStateError)
    except OSError:
        return False  # `.git`이 없는 채워진 폴더는 ensure_clone이 거부한다
    try:
        os.lstat(directory / ".git" / "HEAD")
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


async def _prepare_config(clone: GitClient, url: str) -> None:
    await disable_symlinks(clone)  # 예전 clone에 이미 풀린 링크 대비
    if await remote_url(clone) != url:
        await set_remote_url(clone, url)


async def _update(clone: GitClient, repo: RepoConfig) -> str:
    # 사본을 바꾸는 단계는 취소돼도 끝까지 간다(도중에 git을 죽이면 잠금·반쯤 바뀐 사본이 남는다).
    await run_to_completion(_prepare_config(clone, repo.url))
    await fetch_branch(clone, repo.branch)  # ref를 원자적으로 갱신하므로 취소를 바로 전달한다
    await run_to_completion(checkout_detached(clone, f"origin/{repo.branch}"))
    return await rev_parse(clone, "HEAD")


async def _sync_one(repo: RepoConfig, directory: Path, git: GitClient) -> str:
    try:
        mode: int | None = os.lstat(directory).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and not stat.S_ISDIR(mode):  # 링크(바깥 clone 가리킴)·파일은 거부
        raise CloneStateError("코드 사본 폴더가 실제 폴더가 아닙니다(링크·파일)")
    directory = Path(os.path.abspath(directory))
    clone = git.at(directory)
    sweep_partials(directory)  # 죽은 프로세스가 남긴 받다 만 조각
    if mode is not None and _is_half_clone(directory):
        clear_children(directory)  # 끊긴 옛 clone: 다시 받는다(링크는 따라가지 않고 지운다)
    if is_fresh(directory):
        await clone_fresh(clone, repo.url, directory)  # 조각 폴더에 받고 다 받으면 제자리에 놓는다
    else:
        await ensure_clone(clone, repo.url)  # 가짜 저장소·채워진 폴더는 CloneStateError
    try:
        return await _update(clone, repo)
    except GitError as exc:
        if exc.kind != "exit" or remove_stale_git_locks(clone, repo.branch) == 0:
            raise
        _log.warning("코드 사본에 남은 git 잠금 파일을 지우고 다시 시도합니다")
        try:
            return await _update(clone, repo)
        except GitError:
            raise exc from None  # 처음 오류를 그대로


async def sync_code_repos(
    *, repos: Sequence[RepoConfig], repos_dir: Path, git: GitClient
) -> CodeSyncResult:
    """`active` 저장소의 코드 사본을 원격 브랜치 끝으로 맞춘다. 실패는 저장소별로 모은다.

    `git`은 템플릿이다 — 저장소마다 `git.at(repos_dir / name)`로 쓴다. `archived`는 폴더도 만들지
    않는다.
    """
    heads: dict[str, str] = {}
    failures: dict[str, str] = {}
    for repo in repos:
        if repo.status != "active":
            continue
        if _REPO_NAME.fullmatch(repo.name) is None:
            failures[clean(repo.name[:MAX_BASIS_NAME_CHARS])] = "저장소 이름이 올바르지 않습니다"
            continue
        try:
            heads[repo.name] = await _sync_one(repo, repos_dir / repo.name, git)
        except (GitError, CloneStateError, ValueError, OSError) as exc:
            failures[repo.name] = safe_text(scrub_paths(str(exc)), FAILURE_CHARS)
    return CodeSyncResult(heads=heads, failures=failures)


# ---------------------------------------------------------------- 코드 사본 시점 기록


def read_code_snapshot(state_dir: Path) -> CodeSnapshot | None:
    """없거나 깨졌으면 `None`이다(예외 없음)."""
    return read_json(state_dir / STATE_FILES.code_snapshot, _SNAPSHOT_ADAPTER, None)


def write_code_snapshot(state_dir: Path, heads: Mapping[str, str], now: datetime) -> None:
    """이전 값에 이번 `heads`를 덮어 합치고 `fetched_at`을 `now`(KST ISO)로 적는다.

    이번에 받지 못한 저장소는 이전 SHA를 그대로 둔다.
    """
    previous = read_code_snapshot(state_dir)
    merged = {**(previous.repos if previous is not None else {}), **heads}
    write_json(
        state_dir / STATE_FILES.code_snapshot,
        CodeSnapshot(fetched_at=kst_iso(now), repos=merged),
    )


# ---------------------------------------------------------------- 봇용 기준 블록


def _label(value: str | None) -> str:
    moment = parse_instant(value)
    if moment is None:
        return NO_RECORD
    parts = kst_parts(moment)
    return f"{parts.date} {parts.time}"


def _sha_list(items: Mapping[str, str]) -> str:
    if not items:
        return NONE_LABEL
    ordered = sorted(items.items())
    shown = [
        f"{clean(name[:MAX_BASIS_NAME_CHARS])} {clean(sha[:SHORT_SHA])}"
        for name, sha in ordered[:MAX_BASIS_REPOS]
    ]
    if len(ordered) > MAX_BASIS_REPOS:
        shown.append(f"외 {len(ordered) - MAX_BASIS_REPOS}개")
    return " · ".join(shown)


def basis_lines(baseline: Baseline | None, snapshot: CodeSnapshot | None) -> list[str]:
    """봇 프롬프트의 `[위키 기준]`·`[코드 사본 시점]` 블록(줄 목록)."""
    base_repos = {} if baseline is None else {n: r.commit for n, r in baseline.repos.items()}
    return [
        "[위키 기준]",
        f"반영 시각: {_label(None if baseline is None else baseline.updated_at)}",
        f"저장소 기준: {_sha_list(base_repos)}",
        "",
        "[코드 사본 시점]",
        f"{_sha_list({} if snapshot is None else snapshot.repos)} "
        f"({_label(None if snapshot is None else snapshot.fetched_at)})",
    ]
