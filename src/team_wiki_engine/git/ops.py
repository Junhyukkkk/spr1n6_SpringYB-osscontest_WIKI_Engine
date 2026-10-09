"""git 작업(clone·fetch·로그·diff·status·commit·push)과 출력 파서.

보안 규칙:
- 사용자(또는 저장소 내용)에서 온 ref·브랜치·URL은 **git을 부르기 전에** 검사한다. 맞지 않으면
  `ValueError`이고 git은 한 번도 실행되지 않는다. ref는 40자 SHA·`HEAD`·안전한 브랜치 이름·
  `origin/<안전한 이름>`만 받는다(옵션처럼 보이는 값·`..`·`^{`·개행은 모두 거부). 오류 메시지에는
  입력 값(URL의 사용자 정보 등)을 싣지 않는다.
- git이 허용하는 곳에서는 사용자 위치 인자 앞에 `--`를 둔다(`clone`·`fetch`·`push`·`log`·`diff`·
  `checkout`·`show`·`remote`). 경로 지정자는 `--literal-pathspecs`로 글자 그대로 본다.
- diff를 읽는 호출은 모두 `--no-ext-diff --no-textconv`를 붙인다(저장소 설정의 외부 diff·textconv
  명령을 실행하지 않는다 — `git/client.py`의 남는 위험 참고).
- `cwd` 자체의 마지막 구성요소가 심볼릭 링크이면(`repos/<이름> -> 바깥 clone`) `ensure_clone`을
  포함한 **모든** 연산이 `CloneStateError`다(`lstat(cwd/.git)`은 링크를 따라가므로 따로 본다). 조상
  경로의 별칭(macOS `/var`)은 허용한다.
- 작업 사본을 받는 **모든** 공개 연산(`ensure_clone` 제외)은 먼저 `cwd/.git`이 **실제 폴더**이고
  `.git/HEAD`가 일반 파일이며 내용이 `ref: refs/…` 또는 SHA 모양인지 `lstat`·읽기로 확인하고
  (링크·파일·빈 폴더·쓰레기 `HEAD`·없음이면 `CloneStateError`), `--git-dir`·`--work-tree`를 명시해서
  부른다. 저장소가 아닌 폴더에서 git이 상위 폴더로 올라가 바깥 저장소(또는 `$HOME`)를
  `clean -ffdx`·`reset --hard`·push·fetch하거나 바깥 저장소의 데이터를 읽어 돌려주는 길을 막는다.
- `commit_all`은 중첩 git 저장소(모드 160000 gitlink)가 올라가면 올린 것을 되돌리고
  `CloneStateError`로 거부한다. 메시지는 65,536바이트(UTF-8) 이하여야 하고 `add -A` 전에 검사한다.
- 파서는 어떤 바이트가 와도 예외를 던지지 않는다. 로그는 `-z`와 NUL 구분(git은 제목·이름에 NUL을
  내지 않는다)이라 제목·작성자 속 `\x1e`·`\x1f`로 가짜 레코드를 만들 수 없다. 경로는
  `surrogateescape`로 읽어 잘못된 UTF-8도 잃지 않는다. **주의:** 그래서 `parse_porcelain_z`·
  `changed_paths`·`diff_names`가 돌려주는 문자열에는 `\\udcXX` 대리 문자가 있을 수 있고
  `.encode("utf-8")`는 `UnicodeEncodeError`를 던진다 — 프롬프트·PR 본문·로그로 내보내는 쪽
  (Task 8·9·11)이 직접 걸러야 한다.
- 남는 위험: 저장소 `.git/config`의 `url.<x>.insteadOf`는 fetch·push에서 그대로 적용된다(고정 설정이
  아니다). `.git/config`에 쓸 수 있어야 심을 수 있으므로 `git/client.py`가 적은 다른 남는 키와 같은
  신뢰 경계다.
"""

import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from team_wiki_engine.git.client import GitClient, GitError, GitOutputTooLarge
from team_wiki_engine.util.fs import find_symlinks
from team_wiki_engine.util.time import parse_instant

BOT_NAME = "team-wiki-bot"
BOT_EMAIL = "team-wiki-bot@users.noreply.github.com"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_SHA = re.compile(r"[0-9a-f]{40}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_REMOTE_HELPER = re.compile(r"[A-Za-z0-9+.-]+::")
_STATUS_CHARS = frozenset(" MTADRCU?!")
_URL_MAX = 2048
_MESSAGE_MAX_BYTES = 65_536
_URL_USERINFO = re.compile(r"://[^/?#]*@")
_HEAD_CONTENT = re.compile(rb"(?:ref: refs/[^\x00\r\n]{1,512}|[0-9a-f]{40}(?:[0-9a-f]{24})?)\n?")
_RESERVED_BRANCHES = frozenset(
    {"HEAD", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD"}
)
_HEX40_ANYCASE = re.compile(r"[0-9a-fA-F]{40}")
_PATHSPEC_MAX = 4096
_NETWORK_TIMEOUT_S = 600.0
_STAT_LIMIT_BYTES = 256 * 1024
_MAX_DIGITS = 15
_LOG_FORMAT = "--format=%H%x00%cI%x00%an%x00%s"
_NO_EXTERNAL = ("--no-ext-diff", "--no-textconv")
_STAT_OMITTED = "(파일 목록도 256KB를 넘어 생략)"


class CloneStateError(Exception):
    """작업 사본의 상태가 안전하게 쓸 수 있는 모양이 아니다(`.git`이 링크·파일·없음 등)."""


# ---------------------------------------------------------------- 형식 검사


def is_sha(value: object) -> bool:
    """정확히 40자 소문자 16진 문자열인가(끝의 개행도 거부)."""
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def is_safe_branch(name: str) -> bool:
    """git이 받아들이고 옵션·경로 탈출처럼 쓸 수 없는 브랜치 이름인가."""
    if not isinstance(name, str) or _BRANCH.fullmatch(name) is None:
        return False
    if ".." in name or "//" in name or "@{" in name:
        return False
    if name.startswith("refs/") or name.upper() in _RESERVED_BRANCHES:
        return False
    if _HEX40_ANYCASE.fullmatch(name):  # SHA와 헷갈리는 이름
        return False
    if name.endswith(("/", ".", ".lock")):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/")
    )


def _valid_ref(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if is_sha(value) or value in ("HEAD", EMPTY_TREE) or is_safe_branch(value):
        return True
    return value.startswith("origin/") and is_safe_branch(value[len("origin/") :])


def _check_ref(value: object) -> str:
    if not isinstance(value, str) or not _valid_ref(value):
        raise ValueError("git ref 형식이 올바르지 않습니다")
    return value


def _check_branch(value: object) -> str:
    if not isinstance(value, str) or not is_safe_branch(value):
        raise ValueError("브랜치 이름 형식이 올바르지 않습니다")
    return value


def _check_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _URL_MAX:
        raise ValueError("저장소 URL 형식이 올바르지 않습니다")
    if _URL_USERINFO.search(value) or not _encodable(value):  # 토큰이 .git/config에 남지 않게
        raise ValueError("저장소 URL 형식이 올바르지 않습니다")
    if value.startswith("-") or any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in value):
        raise ValueError("저장소 URL 형식이 올바르지 않습니다")
    lowered = value.lower()
    if lowered.startswith(("ext::", "fd::")) or _REMOTE_HELPER.match(value):
        raise ValueError("저장소 URL 형식이 올바르지 않습니다")
    return value


def _encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _check_pathspec(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or len(value) > _PATHSPEC_MAX
        or not _encodable(value)
    ):
        raise ValueError("경로 지정자 형식이 올바르지 않습니다")
    return value


def _check_message(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("커밋 메시지 형식이 올바르지 않습니다")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("커밋 메시지 형식이 올바르지 않습니다") from None
    if size > _MESSAGE_MAX_BYTES:
        raise ValueError("커밋 메시지가 너무 깁니다")
    return value


# ---------------------------------------------------------------- 작업 사본 보호


def _describe(path: Path | str) -> str:
    return repr(str(path))[:200]


def _check_real_root(root: str) -> None:
    """`root`의 **마지막 구성요소**가 링크가 아닌지 본다(없으면 통과 — 뒤 검사가 따로 거부한다).

    `lstat(root/.git)`은 `root` 자체가 바깥 clone을 가리키는 링크여도 따라가서 정상으로 보이므로
    따로 본다. `realpath(root) == realpath(부모)/이름`이면 마지막 구성요소가 링크가 아니다. 조상의
    별칭(macOS의 `/var` → `/private/var` 등)은 허용한다.
    """
    try:
        mode = os.lstat(root).st_mode
    except OSError:
        return
    parent, name = os.path.split(root)
    if stat.S_ISLNK(mode) or os.path.realpath(root) != os.path.join(os.path.realpath(parent), name):
        raise CloneStateError(f"{_describe(root)}은(는) 링크라서 작업 사본으로 쓰지 않습니다")


def _worktree_scope(git: GitClient) -> list[str]:
    """`cwd/.git`이 실제 저장소 폴더인지 확인하고 `--git-dir`·`--work-tree` 인자를 돌려준다."""
    root = os.path.abspath(git.cwd)
    _check_real_root(root)
    dot_git = os.path.join(root, ".git")
    try:
        mode = os.lstat(dot_git).st_mode
        head_mode = os.lstat(os.path.join(dot_git, "HEAD")).st_mode
    except OSError:
        raise CloneStateError(
            f"{_describe(root)}은(는) git 작업 사본이 아닙니다(.git이 없거나 읽을 수 없음)"
        ) from None
    if not stat.S_ISDIR(mode) or not stat.S_ISREG(head_mode):
        raise CloneStateError(
            f"{_describe(root)}의 .git이 실제 저장소 폴더가 아닙니다(링크·파일·빈 폴더)"
        )
    try:
        fd = os.open(os.path.join(dot_git, "HEAD"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            head = os.read(fd, 1024)
        finally:
            os.close(fd)
    except OSError:
        raise CloneStateError(f"{_describe(root)}의 .git/HEAD를 읽을 수 없습니다") from None
    if _HEAD_CONTENT.fullmatch(head) is None:
        raise CloneStateError(f"{_describe(root)}의 .git/HEAD 내용이 올바르지 않습니다")
    return [f"--git-dir={dot_git}", f"--work-tree={root}"]


def clone_git_dir(git: GitClient) -> str:
    """검증한 작업 사본의 `.git` 폴더 경로. 링크·가짜 저장소·이상한 `HEAD`면 `CloneStateError`."""
    _worktree_scope(git)
    return os.path.join(os.path.abspath(git.cwd), ".git")


_NO_OPTIONAL_LOCKS = "--no-optional-locks"
"""인덱스를 새로 쓰려는 읽기 전용 명령(`status`)이 선택적 락을 쥐지 않게 한다.

`GIT_OPTIONAL_LOCKS=0`(`git_env`)과 이중이다. 커밋 대 커밋 `diff`·`log`·`rev-parse`는 인덱스를
건드리지 않아 붙이지 않는다."""


def _read_scope(git: GitClient) -> list[str]:
    return [*_worktree_scope(git), _NO_OPTIONAL_LOCKS]


async def _clean(git: GitClient, scope: Sequence[str]) -> None:
    # 무시된 파일(.DS_Store 등)과 중첩 저장소까지 지운다(G9).
    await git.run([*scope, "clean", "-q", "-f", "-f", "-d", "-x"])


# ---------------------------------------------------------------- clone·원격


async def ensure_clone(git: GitClient, url: str) -> None:
    """`git.cwd`에 `url`을 clone한다(`core.symlinks=false`). 이미 저장소면 아무것도 안 한다."""
    _check_url(url)
    cwd = Path(os.path.abspath(git.cwd))
    _check_real_root(str(cwd))
    dot_git = cwd / ".git"
    try:
        mode: int | None = os.lstat(dot_git).st_mode
    except FileNotFoundError:
        mode = None
    except OSError:
        raise CloneStateError(f"{_describe(cwd)}의 .git을 읽을 수 없습니다") from None
    if mode is not None:
        if stat.S_ISDIR(mode):
            return
        raise CloneStateError(f"{_describe(cwd)}의 .git이 실제 폴더가 아닙니다(링크·파일)")
    try:
        cwd_mode: int | None = os.lstat(cwd).st_mode
    except FileNotFoundError:
        cwd_mode = None
    except OSError:
        raise CloneStateError(f"{_describe(cwd)}을(를) 읽을 수 없습니다") from None
    if cwd_mode is not None:
        if not stat.S_ISDIR(cwd_mode):
            raise CloneStateError(f"{_describe(cwd)}은(는) 폴더가 아닙니다")
        try:
            with os.scandir(cwd) as entries:
                occupied = next(entries, None) is not None
        except OSError:
            raise CloneStateError(f"{_describe(cwd)}을(를) 읽을 수 없습니다") from None
        if occupied:
            raise CloneStateError(f"{_describe(cwd)}에는 .git이 없는데 폴더가 비어 있지 않습니다")
    cwd.parent.mkdir(parents=True, exist_ok=True)
    await git.at(cwd.parent).run(
        ["clone", "-q", "--no-tags", "-c", "core.symlinks=false", "--", url, str(cwd)],
        timeout_s=_NETWORK_TIMEOUT_S,
    )


async def disable_symlinks(git: GitClient) -> None:
    """`core.symlinks=false`를 쓰고 작업 사본의 심볼릭 링크(`.git/` 아래 제외)를 모두 지운다.

    설정만 바꾸면 이미 링크로 풀린 파일은 `checkout --force`·`reset --hard` 뒤에도 링크로 남는다.
    지운 추적 파일은 이어지는 `checkout_detached`/`reset_branch_to`가 링크 대상 글을 담은 일반
    파일로 다시 만든다.
    """
    scope = _worktree_scope(git)
    await git.run([*scope, "config", "core.symlinks", "false"])
    root = Path(os.path.abspath(git.cwd))
    for link in find_symlinks(root):
        try:
            parts = link.relative_to(root).parts
        except ValueError:
            continue
        if parts and parts[0] == ".git":
            continue
        try:
            os.unlink(link)
        except FileNotFoundError:
            pass


async def remote_url(git: GitClient) -> str | None:
    scope = _worktree_scope(git)
    result = await git.run([*scope, "config", "--get", "remote.origin.url"], allow_codes=(1,))
    if result.code != 0:
        return None
    return result.text.strip() or None


async def set_remote_url(git: GitClient, url: str) -> None:
    _check_url(url)
    scope = _worktree_scope(git)
    if await remote_url(git) is None:
        await git.run([*scope, "remote", "add", "origin", "--", url])
    else:
        await git.run([*scope, "remote", "set-url", "origin", "--", url])


# ---------------------------------------------------------------- fetch·rev-parse·checkout


async def fetch_branch(git: GitClient, branch: str) -> None:
    _check_branch(branch)
    scope = _worktree_scope(git)
    await git.run(
        [
            *scope,
            "fetch",
            "-q",
            "--prune",
            "--no-tags",
            "--",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ],
        timeout_s=_NETWORK_TIMEOUT_S,
    )


async def rev_parse(git: GitClient, ref: str) -> str:
    _check_ref(ref)
    scope = _worktree_scope(git)
    sha = (await git.run([*scope, "rev-parse", "--verify", f"{ref}^{{commit}}"])).text.strip()
    if not is_sha(sha):
        raise GitError("git rev-parse 실패: 예상하지 못한 출력", kind="exit", code=0)
    return sha


async def try_rev_parse(git: GitClient, ref: str) -> str | None:
    """커밋으로 풀리면 SHA, 아니면 `None`.

    `^{commit}`이 없으면 없는 40자 SHA도 종료 코드 0이라 "기준 커밋 사라짐"을 놓친다.
    """
    _check_ref(ref)
    scope = _worktree_scope(git)
    result = await git.run(
        [*scope, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], allow_codes=(1, 128)
    )
    sha = result.text.strip()
    return sha if result.code == 0 and is_sha(sha) else None


async def checkout_detached(git: GitClient, ref: str) -> None:
    _check_ref(ref)
    scope = _worktree_scope(git)
    await git.run([*scope, "checkout", "-q", "--force", "--detach", ref, "--"])
    await _clean(git, scope)


async def reset_branch_to(git: GitClient, branch: str, ref: str) -> None:
    _check_branch(branch)
    _check_ref(ref)
    scope = _worktree_scope(git)
    await git.run([*scope, "checkout", "-q", "--force", "-B", branch, ref, "--"])
    await _clean(git, scope)


async def discard_changes(git: GitClient) -> None:
    scope = _worktree_scope(git)
    await git.run([*scope, "reset", "-q", "--hard", "HEAD"])
    await _clean(git, scope)


# ---------------------------------------------------------------- 로그


@dataclass(frozen=True)
class GitLogEntry:
    sha: str
    author: str
    committed_at: str
    subject: str


def parse_log_records(raw: bytes) -> list[GitLogEntry]:
    """`log -z --format=%H%x00%cI%x00%an%x00%s` 출력을 읽는다. 예외를 던지지 않는다.

    NUL로 나눠 네 칸씩 읽는다(필드 사이와 레코드 끝이 모두 NUL이고 git은 제목·이름에 NUL을
    내지 않으므로 `\\x1e`·`\\x1f`·`|` 같은 글자는 평범한 글이다). SHA가 아닌 칸은 건너뛰어 다음
    칸에서 다시 맞추고, 끝에서 잘린(레코드 끝 NUL이 없는) 항목은 버린다. 디코딩은
    `errors="replace"`다.
    """
    tokens = raw.split(b"\0")
    entries: list[GitLogEntry] = []
    index = 0
    while index + 4 < len(tokens):  # 네 칸 + 레코드 끝 NUL 뒤의 칸(마지막이면 빈 칸)
        sha = tokens[index].decode("utf-8", errors="replace").lstrip(" \t\r\n")
        if not is_sha(sha):
            index += 1
            continue
        committed_at, author, subject = (
            tokens[index + offset].decode("utf-8", errors="replace") for offset in (1, 2, 3)
        )
        entries.append(GitLogEntry(sha, author, committed_at, subject))
        index += 4
    return entries


async def first_parent_log(git: GitClient, from_: str | None, to: str) -> list[GitLogEntry]:
    if from_ is not None:
        _check_ref(from_)
    _check_ref(to)
    scope = _worktree_scope(git)
    span = to if from_ is None else f"{from_}..{to}"
    result = await git.run(
        [
            *scope,
            "log",
            "--no-show-signature",
            "-z",
            "--first-parent",
            "--reverse",
            _LOG_FORMAT,
            span,
            "--",
        ]
    )
    return parse_log_records(result.stdout)


async def commit_date(git: GitClient, sha: str) -> datetime:
    _check_ref(sha)
    scope = _worktree_scope(git)
    text = (
        await git.run([*scope, "show", "--no-show-signature", "-s", "--format=%cI", sha, "--"])
    ).text.strip()
    parsed = parse_instant(text)
    if parsed is None:
        raise ValueError("커밋 날짜를 읽을 수 없습니다")
    return parsed


def _since_argument(since: datetime) -> str:
    """git이 기계의 지역 시각으로 읽지 않도록 UTC 오프셋이 붙은 초 단위 ISO 문자열로 바꾼다."""
    aware = since.replace(tzinfo=UTC) if since.tzinfo is None else since  # naive는 UTC(P9)
    return aware.astimezone(UTC).replace(microsecond=0).isoformat()


async def author_names(git: GitClient, ref: str, since: datetime) -> list[str]:
    _check_ref(ref)
    if not isinstance(since, datetime):
        raise ValueError("since는 datetime이어야 합니다")
    try:
        since_text = _since_argument(since)
    except (ValueError, OverflowError):
        raise ValueError("since를 UTC로 바꿀 수 없습니다") from None
    scope = _worktree_scope(git)
    result = await git.run(
        [
            *scope,
            "log",
            "--no-show-signature",
            "-z",
            f"--since={since_text}",
            "--format=%an",
            ref,
            "--",
        ]
    )
    names = (name.strip() for name in result.text.split("\0"))
    return [name for name in names if name]


# ---------------------------------------------------------------- 상태·커밋·push


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def parse_porcelain_z(raw: bytes) -> list[str]:
    """`status --porcelain=v1 -z` 출력의 경로 목록(순서 유지, 중복 제거). 예외를 던지지 않는다.

    항목마다 앞 3바이트(`XY `) 뒤가 경로다. `XY`에 `R`·`C`가 있으면 다음 토큰(옛 경로)도 넣는다.
    형식이 깨진 토큰은 건너뛴다.
    """
    tokens = raw.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4 or token[2] != 0x20:
            continue
        x, y = chr(token[0]), chr(token[1])
        if x not in _STATUS_CHARS or y not in _STATUS_CHARS:
            continue
        paths.append(_decode_path(token[3:]))
        if "R" in (x, y) or "C" in (x, y):
            if index < len(tokens):
                original = tokens[index]
                index += 1
                if original:
                    paths.append(_decode_path(original))
    return list(dict.fromkeys(paths))


async def changed_paths(git: GitClient) -> list[str]:
    """바뀐·새·삭제·이름 바꾼 경로와 **무시된 파일**의 목록(G5)."""
    scope = _read_scope(git)
    result = await git.run(
        [*scope, "status", "--porcelain=v1", "-z", "-uall", "--ignored=matching"]
    )
    return parse_porcelain_z(result.stdout)


def _has_mode(raw: bytes, modes: frozenset[bytes]) -> bool:
    """`diff --cached --raw -z` 출력에 새 모드가 `modes`에 드는 항목이 있는가."""
    for token in raw.split(b"\0"):
        if token.startswith(b":"):
            fields = token.split(b" ")
            if len(fields) >= 2 and fields[1] in modes:
                return True
    return False


def _has_gitlink(raw: bytes) -> bool:
    """새 모드가 160000(gitlink)인 항목이 있는가."""
    return _has_mode(raw, frozenset({b"160000"}))


async def commit_all(git: GitClient, message: str) -> str | None:
    """바뀐 것을 전부 봇 작성자로 커밋하고 새 SHA를 돌려준다. 바뀐 게 없으면 `None`."""
    _check_message(message)
    scope = _worktree_scope(git)
    await git.run([*scope, "add", "-A"])
    staged = await git.run([*scope, "diff", "--cached", "--quiet", *_NO_EXTERNAL], allow_codes=(1,))
    if staged.code == 0:
        return None
    raw = await git.run([*scope, "diff", "--cached", "--raw", "-z", "--no-renames", *_NO_EXTERNAL])
    if _has_gitlink(raw.stdout):
        await git.run([*scope, "reset", "-q"])  # 올려 둔 것을 되돌린다
        raise CloneStateError(
            f"{_describe(os.path.abspath(git.cwd))}에 중첩 git 저장소가 있어 커밋하지 않았습니다"
        )
    # 작성자는 비밀값이 아니므로 argv로 둔다(P3는 인증값만 금지한다).
    await git.run(
        [
            *scope,
            "-c",
            f"user.name={BOT_NAME}",
            "-c",
            f"user.email={BOT_EMAIL}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            message,
        ]
    )
    return await rev_parse(git, "HEAD")


async def commit_paths(git: GitClient, paths: Sequence[str], message: str) -> str | None:
    """`paths`만 봇 작성자로 커밋하고 새 SHA를 돌려준다. 그 경로에 바뀐 게 없으면 `None`.

    다른 바뀐 파일(올려 둔 것 포함)은 커밋에 섞지 않고 그대로 둔다 — 스크립트가 자기 파일
    (`baseline.json`, `wiki/log.md`)만 커밋하고 싶을 때 쓴다. 경로는 글롭이 아닌 글자 그대로
    읽는다(`--literal-pathspecs`). 중첩 git 저장소(gitlink)와 심볼릭 링크는 거부한다.
    """
    _check_message(message)
    specs = [_check_pathspec(path) for path in paths]
    if not specs:
        raise ValueError("커밋할 경로가 없습니다")
    scope = [*_worktree_scope(git), "--literal-pathspecs"]
    await git.run([*scope, "add", "-A", "--", *specs])
    staged = await git.run(
        [*scope, "diff", "--cached", "--quiet", *_NO_EXTERNAL, "--", *specs], allow_codes=(1,)
    )
    if staged.code == 0:
        return None
    raw = await git.run(
        [*scope, "diff", "--cached", "--raw", "-z", "--no-renames", *_NO_EXTERNAL, "--", *specs]
    )
    if _has_mode(raw.stdout, frozenset({b"160000", b"120000"})):  # 중첩 저장소·심볼릭 링크
        await git.run([*scope, "reset", "-q", "--", *specs])
        raise CloneStateError(
            f"{_describe(os.path.abspath(git.cwd))}에 중첩 git 저장소나 링크가 있어 "
            "커밋하지 않았습니다"
        )
    await git.run(
        [
            *scope,
            "-c",
            f"user.name={BOT_NAME}",
            "-c",
            f"user.email={BOT_EMAIL}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--no-verify",
            "--only",
            "-m",
            message,
            "--",
            *specs,
        ]
    )
    return await rev_parse(git, "HEAD")


async def push(git: GitClient, local_ref: str, remote_branch: str) -> None:
    """강제가 아닌 push — 빨리 감기가 아니면 `GitError`."""
    _check_ref(local_ref)
    _check_branch(remote_branch)
    scope = _worktree_scope(git)
    await git.run(
        [
            *scope,
            "push",
            "-q",
            "--no-verify",
            "--",
            "origin",
            f"{local_ref}:refs/heads/{remote_branch}",
        ],
        timeout_s=_NETWORK_TIMEOUT_S,
    )


async def is_ancestor(git: GitClient, ancestor: str, descendant: str) -> bool:
    _check_ref(ancestor)
    _check_ref(descendant)
    scope = _worktree_scope(git)
    result = await git.run(
        [*scope, "merge-base", "--is-ancestor", ancestor, descendant], allow_codes=(1,)
    )
    return result.code == 0


# ---------------------------------------------------------------- diff


async def diff_names(git: GitClient, from_: str, to: str, pathspecs: Sequence[str]) -> list[str]:
    _check_ref(from_)
    _check_ref(to)
    specs = [_check_pathspec(spec) for spec in pathspecs]
    scope = _worktree_scope(git)
    result = await git.run(
        [
            *scope,
            "--literal-pathspecs",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            *_NO_EXTERNAL,
            from_,
            to,
            "--",
            *specs,
        ]
    )
    return [_decode_path(name) for name in result.stdout.split(b"\0") if name]


@dataclass(frozen=True)
class NumStat:
    files: int
    additions: int
    deletions: int


def _count(raw: bytes) -> int:
    # 바이너리(`-`)·숫자 아닌 값·터무니없이 긴 값은 0줄이다(정수 변환 길이 제한을 건드리지 않는다).
    return int(raw) if raw.isdigit() and len(raw) <= _MAX_DIGITS else 0


def parse_numstat_z(raw: bytes) -> NumStat:
    """`diff --numstat --no-renames -z` 출력의 합계. 예외를 던지지 않는다.

    바이너리(`-\\t-`)는 파일만 세고 줄은 0이다. 경로 칸이 없는 토큰은 건너뛴다.
    """
    files = additions = deletions = 0
    for token in raw.split(b"\0"):
        parts = token.split(b"\t", 2)
        if len(parts) != 3 or not parts[2]:
            continue
        files += 1
        additions += _count(parts[0])
        deletions += _count(parts[1])
    return NumStat(files, additions, deletions)


async def numstat(git: GitClient, from_: str, to: str, pathspec: str) -> NumStat:
    _check_ref(from_)
    _check_ref(to)
    spec = _check_pathspec(pathspec)
    scope = _worktree_scope(git)
    result = await git.run(
        [
            *scope,
            "--literal-pathspecs",
            "diff",
            "--numstat",
            "--no-renames",
            "-z",
            *_NO_EXTERNAL,
            from_,
            to,
            "--",
            spec,
        ]
    )
    return parse_numstat_z(result.stdout)


@dataclass(frozen=True)
class DiffText:
    text: str
    truncated: bool


async def diff_text(git: GitClient, from_: str | None, to: str, max_bytes: int) -> DiffText:
    """`from_`(없으면 빈 트리)에서 `to`까지의 diff. `max_bytes`를 넘으면 파일 목록만 돌려준다."""
    if from_ is not None:
        _check_ref(from_)
    _check_ref(to)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes는 0 이상의 정수여야 합니다")
    base = EMPTY_TREE if from_ is None else from_
    scope = _worktree_scope(git)
    quote = [*scope, "-c", "core.quotepath=false"]  # 한글 경로를 8진 이스케이프로 찍지 않는다
    try:
        result = await git.run(
            [*quote, "diff", "--no-color", *_NO_EXTERNAL, base, to, "--"],
            max_output_bytes=max_bytes,
        )
        return DiffText(result.text, False)
    except GitOutputTooLarge:
        pass
    try:
        listing = (
            await git.run(
                [*quote, "diff", "--stat=200", "--no-color", *_NO_EXTERNAL, base, to, "--"],
                max_output_bytes=_STAT_LIMIT_BYTES,
            )
        ).text
    except GitOutputTooLarge:
        listing = _STAT_OMITTED
    return DiffText(
        f"(diff가 {max_bytes}바이트를 넘어 파일 목록만 적었습니다. "
        f"필요한 파일은 코드 사본에서 직접 읽으세요.)\n\n{listing}",
        True,
    )
