"""저장소별 변경분(`RepoDelta`)과 코드 반영 프롬프트용 JSON(`delta_json`).

PR 제목·본문, 커밋 제목, 작성자 이름, 바뀐 파일 경로는 모두 **공격자가 영향을 줄 수 있는 글**이고
LLM 프롬프트와 JSON으로 들어간다. 그래서:

- 길이를 자르고(G15: 제목·커밋 제목 300자, 본문 4000자, 파일 경로 300자·300개, 직접 커밋 200개,
  머지 PR 100개 — 빠진 개수는 `omitted_*`에 적는다) 한 줄짜리 필드는 `clean()`으로 제어·방향 문자를
  눈에 보이게 이스케이프한다. 본문은 줄바꿈을 살려야 하므로 외톨이 서로게이트만 바꾼다.
- `delta_json`은 직렬화된 JSON에서 제어·서식·방향·줄 구분 문자·서로게이트를 `\\uXXXX`로 바꾸고
  백틱(U+0060)을 `\\u0060`으로 바꾼다 — 결과는 여전히 유효한 JSON이고 `json.loads`가 원래 값을
  돌려주며, 프롬프트의 코드 펜스를 깨거나 줄을 위조할 수 없다(S4).
- 경로 문자열(`local_path`, `diff_file`)은 NFC다. diff 파일은 `run_dir/<이름>.diff`이고 이름이
  `run_dir` 밖으로 나갈 수 없는 모양이 아니면 `ValueError`다(링크로 바꿔 둔 파일은 대상을 건드리지
  않고 새 파일로 바꿔 넣는다). `run_dir`는 0700, diff 파일은 0600으로 만든다.
- 알려진 한계: 리베이스로 머지한 PR은 `merge_commit_sha`가 마지막(끝) 커밋 하나뿐이라 그 앞
  커밋들은 직접 커밋으로 나올 수 있다(`GitHubPort`에 PR의 커밋 목록 엔드포인트가 없다).
- GitHub 쪽 실패는 경고로 바꿔 진행한다: `GitHubError`는 `safe_text`로 가린 메시지를 싣고, 그 밖의
  예외(포트 계약 위반)는 종류 이름만 싣는다(내용에 무엇이 들어 있는지 모른다). `CancelledError`는
  삼키지 않는다. git 쪽 실패(`GitError` 등)는 그대로 올라간다 — 호출자(코드 반영 단계)가 그 저장소를
  실패로 처리한다.
"""

import contextlib
import json
import os
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from team_wiki_engine.config.schema import RepoConfig
from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import (
    GitLogEntry,
    author_names,
    commit_date,
    diff_text,
    first_parent_log,
    rev_parse,
)
from team_wiki_engine.github.client import (
    GitHubError,
    GitHubPort,
    PullSummary,
    RepoRef,
    parse_repo_url,
)
from team_wiki_engine.util.time import parse_instant
from team_wiki_engine.wiki.lint_secrets import clean, safe_text

MAX_DIRECT_COMMITS = 200
MAX_MERGED_PRS = 100
MAX_PR_BODY_CHARS = 4000
MAX_TITLE_CHARS = 300
MAX_CHANGED_FILES = 300
OWNER_WINDOW_DAYS = 90

_MAX_PATH_CHARS = 300
_MAX_NAME_CHARS = 100
_MAX_STAMP_CHARS = 64
_WARNING_CHARS = 300
_CLOSED_PAGES_FIRST = 10
_CLOSED_PAGES_LATER = 5
_AUTHOR_PAGES = 5
_OWNER_COUNT = 2
_DAY = timedelta(days=1)
_EARLIEST = datetime.min.replace(tzinfo=UTC)
_FILE_NAME = re.compile(r"[a-z0-9-]{1,100}")  # 설정의 저장소 이름 규칙(`RepoName`)과 같다
_BODY_BAD = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


# ---------------------------------------------------------------- 모델


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class MergedPr(_Model):
    number: int
    title: str
    body: str
    author: str
    merged_at: str
    changed_files: list[str]


class DirectCommit(_Model):
    sha: str
    subject: str
    author: str


class OpenPr(_Model):
    number: int
    title: str
    author: str
    draft: bool
    updated_at: str


class RepoDelta(_Model):
    name: str
    local_path: str
    from_: str | None = Field(alias="from")  # None이면 처음 읽는 저장소(전체 이력)
    to: str
    diff_file: str
    diff_truncated: bool
    merged_pull_requests: list[MergedPr]
    direct_commits: list[DirectCommit]
    open_pull_requests: list[OpenPr]
    owners: list[str]
    omitted_direct_commits: int = 0
    omitted_merged_pull_requests: int = 0


class QuestionGap(_Model):
    asked_at: str
    question: str
    answer_summary: str
    code_paths: list[str]


class DeltaSummary(_Model):
    run_id: str
    repos: list[RepoDelta]
    archived_repos: list[str]
    question_gaps: list[QuestionGap]


@dataclass(frozen=True)
class RepoDeltaResult:
    delta: RepoDelta
    warnings: tuple[str, ...]


# ---------------------------------------------------------------- 직렬화

_JSON_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# JSON 구조의 줄바꿈(`\n`)을 뺀 ASCII 제어 문자와 ASCII가 아닌 글자 묶음
_JSON_RUN = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]|[^\x00-\x7f]+")


def _json_escape(char: str) -> str:
    code = ord(char)
    if code < 0x10000:
        return f"\\u{code:04x}"
    offset = code - 0x10000
    return f"\\u{0xD800 + (offset >> 10):04x}\\u{0xDC00 + (offset & 0x3FF):04x}"


def _escape_run(match: re.Match[str]) -> str:
    run = match.group()
    if run.isprintable():
        return run
    return "".join(
        _json_escape(c) if unicodedata.category(c) in _JSON_UNSAFE_CATEGORIES else c for c in run
    )


def delta_json(summary: DeltaSummary) -> str:
    """프롬프트에 넣는 JSON(키는 camelCase, `from`은 처음이면 `null`).

    백틱과 위험한 문자는 JSON 유니코드 이스케이프로 바꾼다. 결과에는 백틱이 하나도 없고 모두
    ASCII 제어 문자·서로게이트·방향 문자 없이 UTF-8로 인코딩할 수 있다.
    """
    text = json.dumps(summary.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
    return _JSON_RUN.sub(_escape_run, text).replace("\x60", "\\u0060")


# ---------------------------------------------------------------- 도우미


def top_n(names: Sequence[str], n: int) -> list[str]:
    """많이 나온 순, 같으면 이름의 코드 포인트 순으로 `n`개."""
    if n <= 0:
        return []
    counts = Counter(names)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [name for name, _ in ordered[:n]]


def open_pr_keys(deltas: Sequence[RepoDelta]) -> dict[str, list[str]]:
    """저장소 이름 → `#<번호>@<갱신 시각>` 목록(열린 PR이 바뀌었는지 비교하는 키)."""
    return {d.name: [f"#{p.number}@{p.updated_at}" for p in d.open_pull_requests] for d in deltas}


def _line(text: str, limit: int) -> str:
    """한 줄짜리 필드: NFC로 바꾸고 자른 뒤 제어·방향 문자를 눈에 보이게 이스케이프한다."""
    return clean(unicodedata.normalize("NFC", text)[:limit])[:limit]


def _path(text: str) -> str:
    return clean(unicodedata.normalize("NFC", text[:_MAX_PATH_CHARS]))[:_MAX_PATH_CHARS]


def _body(text: str) -> str:
    """본문: 자르고 제어·서식·사용자 지정·미할당 문자와 서로게이트를 U+FFFD로 바꾼다.

    줄바꿈·탭은 살린다(JSON 이스케이프가 맡는다). 바꿔 두면 `delta_json`의 `\\uXXXX` 이스케이프로
    글이 6배씩 부풀지 않는다.
    """
    text = text[:MAX_PR_BODY_CHARS]
    if text.isprintable():
        return text
    return "".join(
        "\ufffd" if c not in "\n\t" and unicodedata.category(c) in _BODY_BAD else c for c in text
    )


def _write_private(file: Path, content: str) -> None:
    """0600 임시 파일에 쓴 뒤 `os.replace`로 바꿔 넣는다(링크는 대상을 건드리지 않고 바뀐다)."""
    tmp = file.with_name(f".{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8", errors="replace"))
        os.replace(tmp, file)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _diff_path(run_dir: Path, name: str) -> Path:
    """`run_dir/<name>.diff` — 이름이 `run_dir` 밖으로 나갈 수 있는 모양이면 `ValueError`."""
    if _FILE_NAME.fullmatch(name) is None:
        raise ValueError("저장소 이름이 diff 파일 이름으로 쓸 수 없는 모양입니다")
    return run_dir / f"{name}.diff"


def _merge_order(pr: PullSummary) -> tuple[datetime, str, int]:
    moment = parse_instant(pr.merged_at)
    return (moment if moment is not None else _EARLIEST, pr.merged_at or "", pr.number)


async def _stop_instant(git: GitClient, sha: str) -> datetime | None:
    """PR 목록을 훑다 멈출 기준: 기준 커밋 시각의 하루 전. 못 구하면 `None`(쪽 수 상한만)."""
    try:
        return await commit_date(git, sha) - _DAY
    except (GitError, ValueError, OverflowError):
        return None


# ---------------------------------------------------------------- 변경분


@dataclass(frozen=True)
class _GitHubPart:
    merged: list[MergedPr]
    omitted_merged: int
    matched: frozenset[str]
    open_prs: list[OpenPr]
    owners: list[str]


async def _github_part(
    *,
    github: GitHubPort,
    ref: RepoRef,
    commits: Sequence[GitLogEntry],
    stop: datetime | None,
    first: bool,
    since: datetime,
) -> _GitHubPart:
    matched: set[str] = set()
    candidates: dict[int, PullSummary] = {}
    if commits:
        shas = {c.sha for c in commits}
        closed = await github.list_pulls(
            ref,
            state="closed",
            max_pages=_CLOSED_PAGES_FIRST if first else _CLOSED_PAGES_LATER,
            stop_when_updated_before=stop,
        )
        for pr in closed:
            if pr.merged_at is None or pr.merge_commit_sha not in shas:
                continue
            matched.add(pr.merge_commit_sha)  # 빠지는 PR의 커밋도 직접 커밋이 아니다
            candidates.setdefault(pr.number, pr)
    ordered = sorted(candidates.values(), key=_merge_order)
    keep = ordered[max(len(ordered) - max(MAX_MERGED_PRS, 0), 0) :]
    merged: list[MergedPr] = []
    for pr in keep:  # 파일 목록은 남기는 PR만 가져온다(호출 수 상한)
        files = await github.list_pull_files(ref, pr.number, MAX_CHANGED_FILES)
        merged.append(
            MergedPr(
                number=pr.number,
                title=_line(pr.title, MAX_TITLE_CHARS),
                body=_body(pr.body),
                author=_line(pr.author, _MAX_NAME_CHARS),
                merged_at=_line(pr.merged_at or "", _MAX_STAMP_CHARS),
                changed_files=[_path(f) for f in files[:MAX_CHANGED_FILES]],
            )
        )
    opened = await github.list_pulls(ref, state="open", max_pages=1)
    open_prs = [
        OpenPr(
            number=pr.number,
            title=_line(pr.title, MAX_TITLE_CHARS),
            author=_line(pr.author, _MAX_NAME_CHARS),
            draft=pr.draft,
            updated_at=_line(pr.updated_at, _MAX_STAMP_CHARS),
        )
        for pr in opened
    ]
    authors = await github.list_commit_authors(ref, since, _AUTHOR_PAGES)
    owners = top_n([_line(a, _MAX_NAME_CHARS) for a in authors if a], _OWNER_COUNT)
    return _GitHubPart(merged, len(ordered) - len(keep), frozenset(matched), open_prs, owners)


async def compute_repo_delta(
    *,
    repo: RepoConfig,
    git: GitClient,
    from_: str | None,
    github: GitHubPort | None,
    run_dir: Path,
    diff_limit: int,
    now: datetime,
) -> RepoDeltaResult:
    """저장소 하나의 `from_`(없으면 처음부터)→`HEAD` 변경분. `git.cwd`가 코드 사본이다."""
    diff_file = _diff_path(run_dir, repo.name)
    to = await rev_parse(git, "HEAD")
    commits = [] if from_ == to else await first_parent_log(git, from_, to)

    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    diff_truncated = False
    diff_body = ""
    if commits:
        result = await diff_text(git, from_, to, diff_limit)
        diff_body, diff_truncated = result.text, result.truncated
    _write_private(diff_file, diff_body)  # 0600, 링크는 대상을 건드리지 않고 바꿔 넣는다

    since = now - timedelta(days=OWNER_WINDOW_DAYS)
    warnings: list[str] = []
    part: _GitHubPart | None = None
    ref = parse_repo_url(repo.url)
    if github is not None and ref is not None:
        stop = None
        if commits and from_ is not None:
            stop = await _stop_instant(git, from_)
        try:
            part = await _github_part(
                github=github, ref=ref, commits=commits, stop=stop, first=from_ is None, since=since
            )
        except GitHubError as exc:
            reason = safe_text(str(exc), _WARNING_CHARS)
            warnings.append(f"{repo.name}: GitHub API 실패로 PR 매칭 없이 진행 ({reason})")
        except Exception as exc:
            warnings.append(
                f"{repo.name}: GitHub API 처리 중 예상하지 못한 오류로 PR 매칭 없이 진행 "
                f"({type(exc).__name__})"
            )

    matched = part.matched if part is not None else frozenset[str]()
    owners = part.owners if part is not None else []
    if not owners:
        names = await author_names(git, "HEAD", since)
        owners = top_n([_line(n, _MAX_NAME_CHARS) for n in names], _OWNER_COUNT)

    direct = [c for c in commits if c.sha not in matched]
    keep = direct[max(len(direct) - max(MAX_DIRECT_COMMITS, 0), 0) :]
    delta = RepoDelta(
        name=repo.name,
        local_path=_nfc(str(git.cwd)),
        **{"from": from_},
        to=to,
        diff_file=_nfc(str(diff_file)),
        diff_truncated=diff_truncated,
        merged_pull_requests=part.merged if part is not None else [],
        direct_commits=[
            DirectCommit(
                sha=c.sha,
                subject=_line(c.subject, MAX_TITLE_CHARS),
                author=_line(c.author, _MAX_NAME_CHARS),
            )
            for c in keep
        ],
        open_pull_requests=part.open_prs if part is not None else [],
        owners=owners,
        omitted_direct_commits=len(direct) - len(keep),
        omitted_merged_pull_requests=part.omitted_merged if part is not None else 0,
    )
    return RepoDeltaResult(delta=delta, warnings=tuple(warnings))
