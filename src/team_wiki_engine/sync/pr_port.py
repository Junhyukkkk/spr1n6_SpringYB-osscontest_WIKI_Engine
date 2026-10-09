"""PR 포트(GitHub · 로컬 파일)와 추적 파일.

개발 중에는 `LocalPrPort`가 `state/local-prs/<run_id>-<kind>.md`에 PR을 적는다. 사람이 검토해서
받아들이면 `main`에 머지해 push하고(엔진은 브랜치 head가 `main`의 조상이 되면 머지로 본다),
받아들이지 않으면 파일의 `status: open`을 `status: rejected`로 바꾼다. 브랜치를 지운 뒤 머지했다면
`status: merged`(선택적으로 `final_head_sha`)로 바꿔도 된다.

**로컬 PR 파일은 사람이 고치는 믿을 수 없는 입력이다.** 읽기는 `MAX_LOCAL_PR_BYTES`로 막고, 링크는
따라가지 않으며(`O_NOFOLLOW`), 일반 파일이 아니면(폴더·이름 있는 파이프) 열지 않고, 이름은
`LOCAL_PR_NAME`에 `fullmatch`하는 것만 본다. 내용이 망가졌거나 이상하면 예외 없이 **열린 PR로**
본다(중복 PR을 만들지도, 가짜 결과를 기록하지도 않는다 — 계획 G20). 파일에서 읽은 브랜치·SHA는
검증을 통과해야만 git에 닿는다.
"""

import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, FailFast, TypeAdapter, ValidationError, field_validator

from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import (
    _worktree_scope,
    fetch_branch,
    is_ancestor,
    is_safe_branch,
    is_sha,
    try_rev_parse,
)
from team_wiki_engine.github.client import GitHubPort, RepoRef
from team_wiki_engine.metrics.record import STATE_FILES, read_json, write_json
from team_wiki_engine.sync.types import SyncKind
from team_wiki_engine.util.fs import write_file_atomic
from team_wiki_engine.util.time import kst_iso
from team_wiki_engine.wiki.frontmatter import parse_frontmatter, serialize_frontmatter

SYNC_LABELS: Final[Mapping[SyncKind, str]] = MappingProxyType(
    {"code": "wiki-sync:code", "ingest": "wiki-sync:ingest"}
)
LINT_FAILED_LABEL: Final = "점검 실패"

LOCAL_PR_NAME: Final = re.compile(r"[0-9]{8}-[0-9]{4}-(code|ingest)\.md")
"""로컬 PR 파일 이름. 반드시 `fullmatch`로 쓴다(`match`는 끝의 줄바꿈을 통과시킨다)."""
MAX_LOCAL_PR_BYTES: Final = 1_048_576
"""로컬 PR 파일 읽기 상한. 넘는 파일은 내용과 상관없이 망가진 것으로 본다."""

_RUN_ID: Final = re.compile(r"[0-9]{8}-[0-9]{4}")
_WORK_BRANCH: Final = re.compile(r"sync/(code|ingest)-[0-9]{8}-[0-9]{4}")
"""동기화가 만드는 작업 브랜치 이름(`sync/<종류>-<run_id>`, `stage.work_branch`와 같은 형식)."""
_GITHUB_PR_ID: Final = re.compile(r"[0-9]{1,15}")
_LABEL_COLORS: Final = {"kind": "0e8a16", "lint": "d93f0b"}
_MAX_LOCAL_TITLE_CHARS: Final = 300
_MAX_LOCAL_BODY_CHARS: Final = 100_000
_BODY_CUT_NOTICE: Final = "\n\n(본문이 길어 여기서 잘렸습니다)"
_FETCH_TIMEOUT_S: Final = 600.0
_SURROGATES: Final = re.compile("[\ud800-\udfff]")


def _scrub(text: str) -> str:
    """UTF-8로 쓸 수 없는 짝 없는 서로게이트를 U+FFFD로 바꾼다."""
    return _SURROGATES.sub("�", text)


@dataclass(frozen=True)
class OpenPrRequest:
    kind: SyncKind
    run_id: str
    branch: str
    base: str
    title: str
    body: str
    lint_failed: bool
    bot_head_sha: str


class TrackedPr(BaseModel):
    """추적 중인 PR. `id`는 GitHub PR 번호 문자열이거나 로컬 PR 파일 이름이다."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore", hide_input_in_errors=True)

    id: str
    kind: SyncKind
    branch: str
    bot_head_sha: str

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        # GitHub PR 번호이거나 로컬 PR 파일 이름이어야 한다(경로 탈출·명령 조각을 막는다).
        if _GITHUB_PR_ID.fullmatch(value) is None and LOCAL_PR_NAME.fullmatch(value) is None:
            raise ValueError("PR id 형식이 올바르지 않습니다")
        return value

    @field_validator("branch")
    @classmethod
    def _safe_branch(cls, value: str) -> str:
        if not is_safe_branch(value):
            raise ValueError("브랜치 이름 형식이 올바르지 않습니다")
        return value

    @field_validator("bot_head_sha")
    @classmethod
    def _sha(cls, value: str) -> str:
        if not is_sha(value):
            raise ValueError("SHA 형식이 올바르지 않습니다")
        return value


@dataclass(frozen=True)
class PrOutcome:
    state: Literal["open", "merged", "rejected"]
    final_head_sha: str | None
    closed_at: str | None


_OPEN = PrOutcome(state="open", final_head_sha=None, closed_at=None)
_REJECTED = PrOutcome(state="rejected", final_head_sha=None, closed_at=None)


class PrPort(Protocol):
    async def find_open(self, kind: SyncKind) -> str | None: ...

    async def open(self, req: OpenPrRequest) -> TrackedPr: ...

    async def outcome(self, pr: TrackedPr) -> PrOutcome: ...

    async def fetch_final_head(self, pr: TrackedPr) -> None: ...


# ---------------------------------------------------------------- 추적 파일

# 항목마다 따로 검증하려고 `list[Any]`로 읽는다(`list[TrackedPr]`로 읽으면 항목 하나가 틀릴 때
# 전체가 버려진다). `FailFast`는 모듈 `metrics.record`의 안내를 따른다.
_TRACKING_ADAPTER: Final[TypeAdapter[list[Any]]] = TypeAdapter(Annotated[list[Any], FailFast()])


def read_tracking(state_dir: Path) -> list[TrackedPr]:
    """추적 파일을 읽는다. 잘못된 항목·같은 id의 뒤쪽 항목은 버리고 예외를 던지지 않는다."""
    raw = read_json(state_dir / STATE_FILES.pr_tracking, _TRACKING_ADAPTER, [])
    found: list[TrackedPr] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            pr = TrackedPr.model_validate(item)
        except (ValidationError, ValueError, RecursionError):
            continue
        if pr.id in seen:
            continue
        seen.add(pr.id)
        found.append(pr)
    return found


def write_tracking(state_dir: Path, prs: Sequence[TrackedPr]) -> None:
    write_json(state_dir / STATE_FILES.pr_tracking, list(prs))


# ---------------------------------------------------------------- 요청 검사


def _check_request(req: OpenPrRequest) -> None:
    """네트워크·파일 쓰기 전에 요청 값을 검사한다. 메시지에 값을 싣지 않는다."""
    if req.kind not in SYNC_LABELS:
        raise ValueError("PR 종류가 올바르지 않습니다")
    if not (isinstance(req.branch, str) and is_safe_branch(req.branch)):
        raise ValueError("PR 브랜치 이름 형식이 올바르지 않습니다")
    if not (isinstance(req.base, str) and is_safe_branch(req.base)):
        raise ValueError("PR 기준 브랜치 이름 형식이 올바르지 않습니다")
    if not is_sha(req.bot_head_sha):
        raise ValueError("봇 head SHA 형식이 올바르지 않습니다")
    if not (isinstance(req.title, str) and isinstance(req.body, str)):
        raise ValueError("PR 제목·본문은 문자열이어야 합니다")
    if not isinstance(req.lint_failed, bool):
        raise ValueError("lint_failed는 불리언이어야 합니다")


def _tracked_of(req: OpenPrRequest, pr_id: str) -> TrackedPr:
    return TrackedPr(id=pr_id, kind=req.kind, branch=req.branch, bot_head_sha=req.bot_head_sha)


def _check_github_id(pr: TrackedPr) -> int:
    if _GITHUB_PR_ID.fullmatch(pr.id) is None:
        raise ValueError("PR id는 숫자여야 합니다")
    return int(pr.id)


# ---------------------------------------------------------------- GitHub


class GitHubPrPort:
    def __init__(self, *, github: GitHubPort, repo: RepoRef, data_git: GitClient) -> None:
        self._github = github
        self._repo = repo
        self._git = data_git
        self.warnings: list[str] = []
        """운영 보고에 실을 수 있는 내용 없는 경고(오류 종류와 PR 번호만 담는다)."""

    async def find_open(self, kind: SyncKind) -> str | None:
        """종류 라벨이 붙었거나, 라벨 달기에 실패했어도 작업 브랜치 이름이 맞는 열린 PR."""
        label = SYNC_LABELS[kind]
        pulls = await self._github.list_pulls(self._repo, state="open", max_pages=1)
        for pull in pulls:
            if label in pull.labels:
                return str(pull.number)
            match = _WORK_BRANCH.fullmatch(pull.head_ref)
            if match is not None and match.group(1) == kind:
                return str(pull.number)
        return None

    async def open(self, req: OpenPrRequest) -> TrackedPr:
        _check_request(req)
        kind_label = SYNC_LABELS[req.kind]
        labels = [kind_label]
        await self._github.ensure_label(self._repo, kind_label, _LABEL_COLORS["kind"])
        if req.lint_failed:
            await self._github.ensure_label(self._repo, LINT_FAILED_LABEL, _LABEL_COLORS["lint"])
            labels.append(LINT_FAILED_LABEL)
        number = await self._github.create_pull(
            self._repo,
            title=_scrub(req.title),
            head=req.branch,
            base=req.base,
            body=_scrub(req.body),
        )
        await self._label_with_retry(number, labels)
        return _tracked_of(req, str(number))

    async def _label_with_retry(self, number: int, labels: Sequence[str]) -> None:
        """라벨 달기는 한 번 더 시도하고, 그래도 실패하면 경고만 남긴다(PR은 이미 만들어졌다).

        raise하면 호출자가 PR을 추적하지 못해 중복 PR이 생긴다. `find_open`이 브랜치 이름으로도
        찾으므로 라벨이 없어도 다음 실행이 이 PR을 건너뛴다.
        """
        error: Exception | None = None
        for _ in range(2):
            try:
                await self._github.add_labels(self._repo, number, labels)
                return
            except Exception as exc:  # 취소(BaseException)는 삼키지 않는다
                error = exc
        self.warnings.append(
            f"PR #{number}에 라벨을 달지 못했습니다({type(error).__name__}) — 브랜치로 찾습니다"
        )

    async def outcome(self, pr: TrackedPr) -> PrOutcome:
        number = _check_github_id(pr)
        pull = await self._github.get_pull(self._repo, number)
        if pull.state == "open":
            return _OPEN
        if pull.merged:
            head = pull.head_sha if is_sha(pull.head_sha) else pr.bot_head_sha
            return PrOutcome("merged", head, pull.closed_at)
        return PrOutcome("rejected", None, pull.closed_at)

    async def fetch_final_head(self, pr: TrackedPr) -> None:
        number = _check_github_id(pr)
        scope = _worktree_scope(self._git)  # 작업 사본이 아니면 상위 저장소로 번지지 않게 막는다
        await self._git.run(
            [
                *scope,
                "fetch",
                "-q",
                "--no-tags",
                "origin",
                f"+refs/pull/{number}/head:refs/remotes/origin/pr-{number}",
            ],
            timeout_s=_FETCH_TIMEOUT_S,
        )


# ---------------------------------------------------------------- 로컬 파일


class _Doc(NamedTuple):
    """읽은 PR 파일. `present=False`는 파일이 없음, `data=None`은 읽을 수 없거나 망가짐."""

    present: bool
    data: dict[str, Any] | None


_MISSING = _Doc(False, None)
_BROKEN = _Doc(True, None)


def _read_doc(path: str) -> _Doc:
    """PR 파일 하나를 안전하게 읽어 frontmatter를 해석한다. 예외를 던지지 않는다.

    없는 파일(경로 일부가 폴더가 아닌 경우 포함)만 `_MISSING`이다. 링크·폴더·이름 있는 파이프·
    상한 초과·읽기 오류·frontmatter 없음/오류는 모두 `_BROKEN`이다.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError):
        return _MISSING
    except (OSError, ValueError):
        return _BROKEN
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LOCAL_PR_BYTES:
            return _BROKEN
        chunks: list[bytes] = []
        remaining = MAX_LOCAL_PR_BYTES + 1  # 읽는 동안 커져도 상한까지만 잡는다
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return _BROKEN
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > MAX_LOCAL_PR_BYTES:
        return _BROKEN
    parsed = parse_frontmatter(data.decode("utf-8", errors="replace"))
    if parsed.error is not None or parsed.data is None:
        return _BROKEN
    return _Doc(True, parsed.data)


class LocalPrPort:
    def __init__(
        self,
        *,
        directory: Path,
        data_git: GitClient,
        base: str,
        now: Callable[[], datetime],
    ) -> None:
        if not (isinstance(base, str) and is_safe_branch(base)):
            raise ValueError("기준 브랜치 이름 형식이 올바르지 않습니다")
        self._dir = directory
        self._git = data_git
        self._base = base
        self._now = now

    def _path(self, name: str) -> str:
        return os.path.join(os.fspath(self._dir), name)

    @staticmethod
    def _check_file_id(pr: TrackedPr) -> None:
        if LOCAL_PR_NAME.fullmatch(pr.id) is None:
            raise ValueError("PR id가 로컬 PR 파일 이름이 아닙니다")

    def _scan(self) -> list[tuple[str, SyncKind]]:
        """이름이 맞는 일반 파일(링크 아님)을 이름 순으로. 폴더가 없거나 못 읽으면 빈 목록."""
        found: list[tuple[str, SyncKind]] = []
        try:
            with os.scandir(self._dir) as entries:
                for entry in entries:
                    match = LOCAL_PR_NAME.fullmatch(entry.name)
                    if match is None:
                        continue
                    try:
                        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    kind: SyncKind = "code" if match.group(1) == "code" else "ingest"
                    found.append((entry.name, kind))
        except OSError:
            return []
        return sorted(found)

    async def _branch_head(self, branch: str) -> str | None:
        try:
            await fetch_branch(self._git, branch)
        except GitError:
            pass  # 지운 브랜치: 마지막으로 받아 둔 값을 쓴다
        return await try_rev_parse(self._git, f"origin/{branch}")

    async def _decide(self, pr: TrackedPr, doc: _Doc) -> PrOutcome:
        if not doc.present:
            return _REJECTED
        data = doc.data
        if data is None:
            return _OPEN
        status = data.get("status")
        if not isinstance(status, str):
            return _OPEN
        if status == "rejected":
            return _REJECTED
        if status not in ("open", "merged"):
            return _OPEN
        if pr.branch == self._base or not is_safe_branch(pr.branch):
            return _OPEN  # 기준 브랜치와 같거나 이상한 이름은 git에 보내지 않는다
        if status == "merged":
            recorded = data.get("final_head_sha")
            if isinstance(recorded, str) and is_sha(recorded):
                return PrOutcome("merged", recorded, None)
            head = await self._branch_head(pr.branch)
            return PrOutcome("merged", head or pr.bot_head_sha, None)
        await fetch_branch(self._git, self._base)
        head = await self._branch_head(pr.branch)
        if head is not None and await is_ancestor(self._git, head, f"origin/{self._base}"):
            return PrOutcome("merged", head, None)
        return _OPEN

    async def find_open(self, kind: SyncKind) -> str | None:
        for name, file_kind in self._scan():
            if file_kind != kind:
                continue
            doc = _read_doc(self._path(name))
            if not doc.present:
                continue  # 훑는 사이 사라졌다
            tracked = self._tracked_from(name, file_kind, doc.data)
            if tracked is None:
                return name  # 망가진 파일은 열린 것으로 본다(git은 부르지 않는다)
            if (await self._decide(tracked, doc)).state == "open":
                return name
        return None

    @staticmethod
    def _tracked_from(name: str, kind: SyncKind, data: dict[str, Any] | None) -> TrackedPr | None:
        if data is None:
            return None
        try:
            return TrackedPr.model_validate(
                {
                    "id": name,
                    "kind": kind,
                    "branch": data.get("branch"),
                    "bot_head_sha": data.get("bot_head_sha"),
                }
            )
        except (ValidationError, ValueError):
            return None

    async def open(self, req: OpenPrRequest) -> TrackedPr:
        _check_request(req)
        if not (isinstance(req.run_id, str) and _RUN_ID.fullmatch(req.run_id)):
            raise ValueError("run_id 형식이 올바르지 않습니다")
        name = f"{req.run_id}-{req.kind}.md"
        if LOCAL_PR_NAME.fullmatch(name) is None:  # 찾을 수 없는 이름은 만들지 않는다
            raise ValueError("로컬 PR 파일 이름이 올바르지 않습니다")
        tracked = _tracked_of(req, name)
        target = self._path(name)
        try:
            existing: int | None = os.lstat(target).st_mode
        except OSError:
            existing = None
        if existing is not None and not stat.S_ISLNK(existing):
            # 덮어쓰면 사람이 바꾼 status가 사라진다(링크는 따라가지 않고 바꾼다).
            raise ValueError("같은 실행의 로컬 PR 파일이 이미 있습니다")
        # U+0085(NEL)는 PyYAML이 따옴표 안에서도 줄바꿈으로 읽어 값이 바뀐다(실측) -> 공백으로.
        title = _scrub(req.title).replace("\x85", " ")[:_MAX_LOCAL_TITLE_CHARS]
        body = _scrub(req.body)
        if len(body) > _MAX_LOCAL_BODY_CHARS:
            body = body[:_MAX_LOCAL_BODY_CHARS] + _BODY_CUT_NOTICE
        meta = {
            "title": title,
            "kind": req.kind,
            "branch": req.branch,
            "base": req.base,
            "status": "open",
            "bot_head_sha": req.bot_head_sha,
            "lint_failed": req.lint_failed,
            "created_at": kst_iso(self._now()),
        }
        write_file_atomic(Path(target), serialize_frontmatter(meta, f"\n{body}\n"))
        return tracked

    async def outcome(self, pr: TrackedPr) -> PrOutcome:
        self._check_file_id(pr)
        return await self._decide(pr, _read_doc(self._path(pr.id)))

    async def fetch_final_head(self, pr: TrackedPr) -> None:
        self._check_file_id(pr)
        if not is_safe_branch(pr.branch):
            raise ValueError("PR 브랜치 이름 형식이 올바르지 않습니다")
        try:
            await fetch_branch(self._git, pr.branch)
        except GitError:
            pass  # 이미 받아 둔 커밋을 쓴다
