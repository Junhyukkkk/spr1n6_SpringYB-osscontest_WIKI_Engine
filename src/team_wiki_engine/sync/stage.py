"""동기화 단계 공통: 의존성 묶음, 단계 문맥, 실패 처리, 잠금 확인 로그 쓰기(G8·G11).

규칙:
- `SyncDeps.redact`는 알려진 비밀값(토큰·키)이다. `repr`에 나오지 않게 `repr=False`로 두고, 운영
  채널에 보낼 모든 메시지는 `safe_message`를 지난다. `SyncDeps`는 환경변수 전체나 토큰을 필드로
  들지 않는다(`data_git`의 `repr`도 인증 여부만 보인다).
- `StageError`의 메시지는 운영 채널에 그대로 써도 안전해야 한다(만드는 쪽이 `safe_message`를 쓴다).
- `append_log_locked`는 `SyncLock`을 **쥔** 문맥에서만 `log.md`를 쓴다. 놓은 잠금이면 아무것도
  쓰지 않고(폴더도 만들지 않는다) `LockNotHeldError`다.
- 코드 반영·문서 반영 단계가 같이 쓰는 도우미(`Mask`, `guard`, `check_script_targets`,
  `fail_without_lock`, `pr_port_warnings`, `RUN_ID_PATTERN`)도 여기 있다.
"""

import os
import re
import stat
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from team_wiki_engine.claude.runner import drain_background_tasks
from team_wiki_engine.claude.types import ClaudeRunner
from team_wiki_engine.config.load import Paths
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import CloneStateError, changed_paths, diff_names
from team_wiki_engine.github.client import GitHubError, GitHubPort
from team_wiki_engine.ops.notify import Notifier, redact_secrets
from team_wiki_engine.sync.claude_step import Drain, discard_quietly
from team_wiki_engine.sync.lock import SyncLock
from team_wiki_engine.sync.pr_port import PrPort
from team_wiki_engine.sync.types import StageResult, SyncKind
from team_wiki_engine.util.aio import run_to_completion
from team_wiki_engine.wiki.baseline import Baseline, read_baseline
from team_wiki_engine.wiki.lint_secrets import clean, safe_text
from team_wiki_engine.wiki.log import LogEntry, append_log

KIND_LABEL: Final[Mapping[SyncKind, str]] = {"code": "코드", "ingest": "문서"}

DIRECT_LINT_MESSAGE: Final = "점검 실패로 main에 직접 반영하지 않음"
NO_LOCK_MESSAGE: Final = "sync.lock 없이 단계를 시작할 수 없습니다"
RUN_ID_PATTERN: Final = re.compile(r"[0-9]{8}-[0-9]{4}")
EXPECTED_ERRORS: Final = (GitError, GitHubError, CloneStateError, OSError, ValueError)
"""단계가 가린 메시지의 `StageError`로 바꾸는 오류(`guard`)."""

PATH_PLACEHOLDER = "<경로>"
DEFAULT_MESSAGE_CHARS = 300
MAX_BASELINE_BYTES = 1024 * 1024
_LOG_FILE = "wiki/log.md"
_WIKI_PREFIX = "wiki/"
_ELLIPSIS = "…"


class StageError(Exception):
    """단계를 실패로 끝내는 오류. 메시지는 운영 채널에 그대로 써도 안전하다."""


class LockNotHeldError(RuntimeError):
    """`sync.lock`을 쥐지 않은 채 잠금이 필요한 쓰기를 하려 했다."""


@dataclass(frozen=True)
class SyncDeps:
    config: TeamConfig
    paths: Paths
    runner: ClaudeRunner
    github: GitHubPort | None
    pr_port: PrPort | None
    data_git: GitClient
    code_git: GitClient
    """코드 사본용 템플릿(인증 없음). 저장소마다 `code_git.at(<코드 사본 폴더>)`로 쓴다."""
    notifier: Notifier
    now: Callable[[], datetime]
    drain: Drain = drain_background_tasks
    settle_timeout_s: float = 30.0
    redact: tuple[str, ...] = field(default=(), repr=False)
    """알려진 비밀값. `repr`·로그에 나오지 않는다."""


@dataclass(frozen=True)
class StageContext:
    deps: SyncDeps
    run_id: str
    now: datetime
    lock: SyncLock


def work_branch(kind: SyncKind, run_id: str) -> str:
    return f"sync/{kind}-{run_id}"


def _drop_partial_escape(text: str) -> str:
    """끝에 잘려 남은 `\\x1`·`\\u20`·`\\` 같은 이스케이프 조각을 뺀다."""
    index = text.rfind("\\")
    if index == -1:
        return text
    tail = text[index:]
    if len(tail) < 10 and (tail == "\\" or tail[1:2] in "xuU"):
        return text[:index]
    return text


_COMPONENT = r"[^\s/'\"`<>|*?:;,()\[\]{}\\]+"
# `/a/b`처럼 구성요소가 둘 이상인 절대 경로. 앞 글자가 낱말·`:`·`/`·`.`·`~`·`>`·`-`이면
# 상대 경로·URL의 일부(`a/b`, `https://x/y`, `../x/y`)이거나 이미 바꾼 표지 뒤라서 건드리지 않는다.
# 구성요소는 `/`를 담지 않으므로 겹치는 수량자가 없어 입력 길이에 선형이다.
_POSIX_PATH = re.compile(rf"(?<![\w:/.~>\-])(?:/{_COMPONENT}){{2,}}")
_DRIVE_PATH = re.compile(r"(?<!\w)[A-Za-z]:[\\/][^\s'\"<>|*?]+")


def path_roots(paths: Paths) -> dict[str, str]:
    """`scrub_paths`에 넘길 설정된 루트: 표지 → 절대 경로."""
    return {
        "<home>": str(paths.home),
        "<engine>": str(paths.engine_dir),
        "<skills>": str(paths.skills_dir),
        "<data>": str(paths.data_dir),
        "<data-sync>": str(paths.data_sync_dir),
        "<repos>": str(paths.repos_dir),
        "<state>": str(paths.state_dir),
        "<runs>": str(paths.runs_dir),
        "<local-prs>": str(paths.local_prs_dir),
    }


_PRIVATE_PREFIXES = (
    "/Users/",
    "/home/",
    "/root/",
    "/tmp/",
    "/private/",
    "/var/",
    "/etc/",
    "/opt/",
    "/srv/",
)


def _scrub_private(match: re.Match[str]) -> str:
    text = match.group()
    return PATH_PLACEHOLDER if text.startswith(_PRIVATE_PREFIXES) else text


def scrub_paths(text: str, roots: Mapping[str, str] | None = None, *, generic: bool = True) -> str:
    """운영 채널·PR 본문에 서버의 절대 경로가 새지 않게 한다.

    1) 설정된 루트(링크를 푼 경로도)는 긴 것부터 짧은 표지(`<repos>` 등)로 바꾸고, 2) 그래도 남은
    절대 경로(`/`로 시작하고 구성요소가 둘 이상, 또는 윈도 드라이브 경로)는 `<경로>`로 바꾼다.
    `wiki/index.md`·`origin/main`·`https://x/y`·`../x` 같은 상대 경로와 URL은 건드리지 않는다.

    `generic=False`는 모델이 쓴 산문용이다: 설정된 루트와 `/Users/`·`/home/`·`/root/`·`/tmp/`·
    `/private/`·`/var/`·`/etc/`·`/opt/`·`/srv/`로 시작하는 경로만 바꾸고
    `/api/v2/members` 같은 일반 절대 경로는 둔다.
    """
    candidates: list[tuple[str, str]] = []
    for token, root in (roots or {}).items():
        if len(root) <= 1:
            continue
        candidates.append((root, token))
        try:
            real = os.path.realpath(root)
        except (OSError, ValueError):
            continue
        if real != root and len(real) > 1:
            candidates.append((real, token))
    for root, token in sorted(candidates, key=lambda item: -len(item[0])):
        text = text.replace(root, token)
    if generic:
        text = _POSIX_PATH.sub(PATH_PLACEHOLDER, text)
    else:  # 모델이 쓴 글: `/api/v2/members` 같은 라우트는 두고 서버 경로 모양만 바꾼다
        text = _POSIX_PATH.sub(_scrub_private, text)
    return _DRIVE_PATH.sub(PATH_PLACEHOLDER, text)


def safe_message(
    text: str,
    redact: Sequence[str],
    limit: int = DEFAULT_MESSAGE_CHARS,
    roots: Mapping[str, str] | None = None,
) -> str:
    """운영 채널에 쓸 메시지: 알려진 비밀값 가림 → 절대 경로 바꾸기 → `safe_text`로 가리고 자르기.

    순서가 중요하다. 자른 뒤에 가리면 경계에 걸친 비밀값 조각이 남는다. `safe_text`는 `…`와
    제어 문자 이스케이프 때문에 `limit`보다 길어질 수 있어서 결과가 **`limit`자 이하**가 되도록 한
    번 더 자른다(`…`로 끝난다).
    """
    if limit <= 0:
        return ""
    shown = safe_text(scrub_paths(redact_secrets(text, redact), roots), limit)
    if len(shown) <= limit:
        return shown
    return _drop_partial_escape(shown[: max(limit - 1, 0)]) + _ELLIPSIS


async def fail_stage(stage: StageResult, git: GitClient, message: str) -> StageResult:
    """작업 사본을 되돌리고(실패는 삼킨다) 단계를 실패로 표시한다. `message`는 안전해야 한다."""
    await discard_quietly(git)
    stage.result = "failed"
    stage.error = message
    stage.summary = f"{KIND_LABEL[stage.kind]}: 실패 — {message}"
    return stage


def load_baseline(data_dir: Path, redact: Sequence[str]) -> Baseline:
    """`baseline.json`을 읽는다. 없으면 빈 기준, 못 읽으면(`ValueError`·`OSError`) `StageError`.

    읽기 전에 `lstat`으로 본다: 일반 파일이 아니거나(링크·폴더·FIFO — 링크 대상을 읽거나 FIFO에서
    멈추지 않게) 1MiB보다 크면 내용을 읽지 않고 `StageError`다(메시지에 경로·내용이 없다).
    """
    try:
        info = os.lstat(data_dir / "baseline.json")
    except OSError:
        info = None  # 없음 → 빈 기준. 그 밖의 오류는 아래 read_baseline이 같은 오류로 드러낸다
    if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BASELINE_BYTES):
        raise StageError("baseline.json을 읽을 수 없습니다: 일반 파일이 아니거나 너무 큽니다")
    try:
        return read_baseline(data_dir)
    except (ValueError, OSError) as exc:  # UnicodeDecodeError는 ValueError, 폴더·권한은 OSError
        raise StageError(
            "baseline.json을 읽을 수 없습니다: " + safe_message(str(exc), redact)
        ) from None


def _page_name(path: str) -> str:
    """로그·PR 본문에 싣는 페이지 이름: NFC로 바꾸고 제어 문자·서로게이트를 이스케이프한다."""
    return clean(unicodedata.normalize("NFC", path))


async def wiki_pages_changed(git: GitClient, base: str) -> list[str]:
    """`origin/<base>`와 `HEAD` 사이에 바뀐 `wiki/` 쪽 페이지(`wiki/` 접두와 `log.md` 제외, NFC).

    git 오류는 그대로 올린다. 이상한 경로(잘못된 UTF-8, 제어 문자, 따옴표)는 던지지 않고
    이스케이프해서 돌려준다(경로는 `-z`로 읽어 git이 따옴표로 감싸지 않는다).
    """
    names = await diff_names(git, f"origin/{base}", "HEAD", ["wiki"])
    return [_page_name(name.removeprefix(_WIKI_PREFIX)) for name in names if name != _LOG_FILE]


def difference(a: Sequence[str], b: Sequence[str]) -> list[str]:
    """`a`에서 `b`에 없는 것(`a`의 순서 유지)."""
    excluded = set(b)
    return [item for item in a if item not in excluded]


def append_log_locked(ctx: StageContext, entry: LogEntry) -> None:
    """`sync.lock`을 쥔 문맥에서만 `wiki/log.md`에 한 줄 붙인다. 아니면 아무것도 쓰기 전에 거부."""
    if not ctx.lock.held:
        raise LockNotHeldError("sync.lock 없이 log.md를 쓰려 했습니다")
    append_log(ctx.deps.paths.data_sync_dir, entry)


# ---------------------------------------------------------------- 단계 공통 도우미


@dataclass(frozen=True)
class Mask:
    """운영 채널·PR 본문에 가는 모든 글의 관문: 비밀값 가림 → 절대 경로 바꾸기 → 자르기."""

    redact: tuple[str, ...]
    roots: Mapping[str, str]

    def __call__(self, text: str) -> str:
        return safe_message(text, self.redact, roots=self.roots)


def make_mask(deps: SyncDeps) -> Mask:
    return Mask(tuple(deps.redact), path_roots(deps.paths))


def describe_error(exc: Exception, mask: Mask) -> str:
    """운영 채널용 오류 설명. 파일 오류(`OSError`)는 문구에 경로가 있어 종류 이름만 싣는다."""
    if isinstance(exc, OSError):
        return type(exc).__name__
    return mask(str(exc))


@contextmanager
def guard(mask: Mask, what: str) -> Iterator[None]:
    """git·GitHub·파일 오류를 가린 메시지의 `StageError`로 바꾼다(원본 예외는 연쇄하지 않는다)."""
    try:
        yield
    except EXPECTED_ERRORS as exc:
        raise StageError(f"{what}: {describe_error(exc, mask)}") from None


def fail_without_lock(stage: StageResult) -> StageResult:
    """잠금 없이는 작업 사본을 되돌리지도 않는다(다른 실행이 쥔 사본일 수 있다)."""
    stage.result = "failed"
    stage.error = NO_LOCK_MESSAGE
    stage.summary = f"{KIND_LABEL[stage.kind]}: 실패 — {NO_LOCK_MESSAGE}"
    return stage


def check_script_targets(
    data_dir: Path, *, dirs: Sequence[str] = (), files: Sequence[str] = ()
) -> None:
    """스크립트가 쓸 곳이 링크·엉뚱한 종류가 아닌지 `lstat`으로 본다(없는 것은 괜찮다)."""
    for rel, want_dir in [(d, True) for d in dirs] + [(f, False) for f in files]:
        try:
            mode = os.lstat(os.path.join(os.fspath(data_dir), rel)).st_mode
        except FileNotFoundError:
            continue
        except OSError:
            raise StageError(f"{rel}를 확인할 수 없습니다") from None
        if stat.S_ISLNK(mode):
            raise StageError(f"{rel}가 심볼릭 링크입니다")
        if not (stat.S_ISDIR(mode) if want_dir else stat.S_ISREG(mode)):
            raise StageError(f"{rel}가 올바른 종류의 파일이 아닙니다")


def pr_port_warnings(pr_port: object) -> list[str]:
    """`GitHubPrPort.warnings`처럼 `warnings` 목록이 있는 포트의 경고(내용 없는 경고)."""
    warnings = getattr(pr_port, "warnings", None)
    if not isinstance(warnings, list):
        return []
    return [w for w in warnings if isinstance(w, str)]


async def ensure_clean_tree(git: GitClient, mask: Mask) -> None:
    """작업 사본에 커밋되지 않은 변경(무시된 파일 포함)이 있으면 `StageError`.

    스크립트가 자기 값을 커밋하기 전에 부른다 — Claude 실행이 앞서지 않은 경로(재확인 대상 없음,
    뒤 원본에서 멈춤)에서도 목록을 만든 뒤 바뀐 `raw/`나 엉뚱한 파일이 전달되지 않게 한다.
    메시지에는 이름·경로를 싣지 않는다.
    """
    with guard(mask, "작업 사본을 확인할 수 없습니다"):
        dirty = await run_to_completion(changed_paths(git))
    if dirty:
        raise StageError("작업 사본에 커밋되지 않은 변경이 있어 중단합니다")
