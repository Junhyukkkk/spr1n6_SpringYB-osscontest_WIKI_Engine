"""Claude가 고친 파일의 사후 검사와, 쓰기를 허용하기 전의 쓰기 루트 사전 검사.

둘 다 **보안 경계**다. 사후 검사가 Claude의 변경을 커밋할지 정하고, 사전 검사가 Claude에게
쓰기를 허용할지 정한다. 그래서 두 함수는 어떤 입력에도 예외를 던지지 않고(적대적 문자열·
`OSError`는 위반·사유로), 모호하면 위반으로 본다.

규칙(`check_changed_paths`): 경로가 아래 중 하나라도 해당하면 위반이다.
- 대리 문자(잘못된 UTF-8)·제어·서식·사설·미할당 문자·줄/문단 구분자가 있다.
- NFC로 바꾼 경로가 `wiki/` 아래가 아니다(`wiki` 자체·빈 경로·절대 경로·`.`·`..`·빈 구성요소 포함).
- 최상위 `wiki/log.md`와 같다(NFC 뒤 `casefold`, 끝의 `/`·공백·`.`은 무시). 전각 `ｌｏｇ．ｍｄ`처럼
  `casefold`로도 같아지지 않는 이름은 다른 파일이라 허용한다.
- 보이지 않는 채움 문자(U+034F, 한글 채움, U+2800 점자 빈칸 등)가 있다. ZWJ(U+200D, Cf)가 든
  이모지 조합과 Python 3.12에 미할당인 Unicode 16 문자도 위반이다(파일 이름은 일반 글자·숫자·
  한글·하이픈으로 쓴다 — `skills/wiki-schema`가 안내한다).
- 구성요소에 `.git`이 있다(NFC·NFKC `casefold`, 끝 공백·점 무시, `git~1`). 또 `wiki` 안에서
  git 동작을 바꾸는 `.gitignore`·`.gitattributes`·`.gitmodules`도 막는다(Claude 파일이 조용히
  커밋에서 빠지거나 git 속성이 바뀌는 길).

`git status`는 링크의 종류(`?? wiki/l.md -> /etc/passwd`)·중첩 저장소(`wiki/x/.git`)·읽을 수 없는
폴더를 위반으로 보여 주지 못한다. 그래서 `verify_claude_changes`는 경로 검사 뒤에 `wiki/`를
**직접 걸어**(`os.scandir`, 링크를 따라가지 않음) 심볼릭 링크·`.git` 이름·읽을 수 없는 폴더도
위반으로 센다. 사전 검사 `check_write_root`도 같은 걷기를 쓴다. 따라서 Task 9의 `run_write_step`은
별도의 `find_symlinks` 단계가 필요 없다(사전 검사 + 사후 검사로 충분하다).
"""

import os
import stat
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from team_wiki_engine.git.client import GitClient
from team_wiki_engine.git.ops import changed_paths, discard_changes
from team_wiki_engine.ops.notify import redact_secrets
from team_wiki_engine.util.aio import run_to_completion
from team_wiki_engine.wiki.lint_secrets import display_name

_WRITABLE_PREFIX = "wiki/"
_SCRIPT_ONLY_LOG = "wiki/log.md"
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_GIT_SPECIAL = frozenset({".gitignore", ".gitattributes", ".gitmodules"})
_INVISIBLE_FILLERS = frozenset(
    "\u034f\u115f\u1160\u17b4\u17b5\u180b\u180c\u180d\u180f\u2800\u3164\uffa0"
)
_MAX_SHOWN_LINKS = 5
_MAX_DISPLAY_CHARS = 300
_MAX_OFFENDING = 50
_MAX_WALK_ENTRIES = 500_000
_MAX_REPAIR_ROUNDS = 20
_OVERFLOW_NAME = "<항목 수 한도 초과>"
_UNCHECKABLE = "<검사할 수 없는 경로>"


@dataclass(frozen=True)
class PathCheck:
    ok: bool
    offending: tuple[str, ...]  # 표시용 경로(가려지고 이스케이프됨)


# ---------------------------------------------------------------- 표시


def _escape_surrogates(text: str) -> str:
    """대리 문자를 `\\xff`(잘못된 UTF-8 바이트)·`\\ud800`꼴로 바꿔 UTF-8로 인코딩할 수 있게 한다."""
    raw = bytearray()
    for char in text:
        code = ord(char)
        if 0xDC80 <= code <= 0xDCFF:  # surrogateescape가 만든 한 바이트
            raw.append(code - 0xDC00)
        elif 0xD800 <= code <= 0xDFFF:
            raw += f"\\u{code:04x}".encode("ascii")
        else:
            raw += char.encode("utf-8")
    return raw.decode("utf-8", errors="backslashreplace")


def _display_path(path: str, redact: Sequence[str] = ()) -> str:
    """대리 문자·제어 문자를 이스케이프하고 비밀값처럼 보이는 구성요소를 가린다.

    알려진 비밀값(`redact`)은 **자르기 전에** 전체 경로에서 가린다(300자 경계에 걸친 조각 방지).
    """
    escaped = redact_secrets(_escape_surrogates(path), redact)
    shown = "/".join(display_name(part) for part in escaped.split("/"))
    if len(shown) > _MAX_DISPLAY_CHARS:
        shown = f"{shown[:_MAX_DISPLAY_CHARS]}…"
    return shown


# ---------------------------------------------------------------- 순수 검사


def _is_unsafe_char(char: str) -> bool:
    return unicodedata.category(char) in _UNSAFE_CATEGORIES or char in _INVISIBLE_FILLERS


def _has_unsafe_char(text: str) -> bool:
    return any(_is_unsafe_char(char) for char in text)


def has_unsafe_char(text: str) -> bool:
    """제어·서식·대리·사설·미할당 문자나 보이지 않는 채움 문자가 있는가(사후 검사와 같은 기준).

    문서 반영 단계가 원본 파일 이름을 거를 때 쓴다 — 사후 검사가 거부할 이름으로는 Claude를
    부르지 않는다.
    """
    return _has_unsafe_char(text)


def _is_git_name(part: str) -> bool:
    for form in ("NFC", "NFKC"):
        folded = unicodedata.normalize(form, part).casefold().rstrip(" .")
        if folded in (".git", "git~1"):
            return True
    return False


def _is_violation(path: object) -> bool:
    if not isinstance(path, str):
        return True
    if _has_unsafe_char(path):
        return True
    nfc = unicodedata.normalize("NFC", path)
    if _has_unsafe_char(nfc) or not nfc.startswith(_WRITABLE_PREFIX):
        return True
    body = nfc[len(_WRITABLE_PREFIX) :]
    parts = body.split("/")
    if parts and parts[-1] == "":  # 무시된 폴더 항목 `wiki/dir/`
        parts.pop()
    if not parts or any(part in ("", ".", "..") for part in parts):
        return True
    if any(_is_git_name(part) or part.casefold() in _GIT_SPECIAL for part in parts):
        return True
    if len(parts) == 1 and parts[0].casefold().rstrip(" .") == _SCRIPT_ONLY_LOG[len("wiki/") :]:
        return True
    return False


def _violation_of(path: object) -> bool:
    try:
        return _is_violation(path)
    except Exception:
        return True


def _display_of(path: object, redact: Sequence[str] = ()) -> str:
    try:
        return _display_path(path, redact) if isinstance(path, str) else _UNCHECKABLE
    except Exception:
        return _UNCHECKABLE


def _violating_paths(items: Iterable[object]) -> list[object]:
    """위반 경로(원본). 같은 문자열은 한 번만 담는다."""
    found: list[object] = []
    seen: set[str] = set()
    for path in items:
        if isinstance(path, str):
            if path in seen:
                continue
            seen.add(path)
        if _violation_of(path):
            found.append(path)
    return found


def _summarize(violations: Sequence[object], redact: Sequence[str] = ()) -> PathCheck:
    """앞 50개만 표시용으로 바꾸고 나머지는 `외 N개`로 줄인다(표시 비용·크기 상한)."""
    unique = [v for v in dict.fromkeys(v for v in violations if isinstance(v, str))]
    others = [v for v in violations if not isinstance(v, str)]
    everything: list[object] = [*unique, *others]
    shown = [_display_of(path, redact) for path in everything[:_MAX_OFFENDING]]
    if len(everything) > _MAX_OFFENDING:
        shown.append(f"외 {len(everything) - _MAX_OFFENDING}개")
    return PathCheck(not everything, tuple(shown))


def _as_items(changed: object) -> Iterable[object]:
    if isinstance(changed, Iterable) and not isinstance(changed, str | bytes):
        return changed
    return [changed]


def check_changed_paths(changed: Sequence[str], redact: Sequence[str] = ()) -> PathCheck:
    """바뀐 경로가 모두 `wiki/` 아래(`wiki/log.md`·`.git` 제외)인지 본다. 예외를 던지지 않는다.

    `offending`은 표시용 경로 앞 50개이고, 더 있으면 마지막에 `외 N개`가 붙는다(`ok`는 False).
    """
    try:
        return _summarize(_violating_paths(_as_items(changed)), redact)
    except Exception:
        return PathCheck(False, (_UNCHECKABLE,))


@dataclass
class _WalkResult:
    symlinks: list[tuple[str, ...]]
    unreadable: list[tuple[str, ...]]
    git_names: list[tuple[str, ...]]
    overflow: bool = False


def _walk_wiki(wiki: str) -> _WalkResult:
    """`wiki`(실제 폴더여야 한다) 아래를 링크를 따라가지 않고 반복문으로 걷는다.

    심볼릭 링크, `.git` 이름(중첩 저장소 — 안은 걷지 않는다), 읽을 수 없는 폴더(`scandir` 실패,
    `wiki` 자체는 빈 튜플)를 모은다. 항목 수가 `_MAX_WALK_ENTRIES`를 넘으면 `overflow`로 멈춘다.
    """
    result = _WalkResult([], [], [], False)
    pending: list[tuple[str, tuple[str, ...]]] = [(wiki, ())]
    visited = 0
    while pending:
        current, parts = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    visited += 1
                    if visited > _MAX_WALK_ENTRIES:
                        result.overflow = True
                        return result
                    here = (*parts, entry.name)
                    try:
                        is_git = _is_git_name(entry.name)
                        is_link = entry.is_symlink()
                        is_dir = not is_link and entry.is_dir(follow_symlinks=False)
                    except (OSError, ValueError):
                        result.unreadable.append(here)
                        continue
                    if is_git:
                        result.git_names.append(here)
                    if is_link:
                        result.symlinks.append(here)
                    elif is_dir and not is_git:
                        pending.append((entry.path, here))
        except OSError:
            result.unreadable.append(parts)
    return result


def _walk_findings(root: str) -> list[object]:
    """`root/wiki`의 걷기 결과를 `wiki/…` 경로 문자열로 돌려준다(없으면 빈 목록)."""
    wiki = os.path.join(root, "wiki")
    mode = _lstat_mode(wiki)
    if mode is None:
        return []
    if _wiki_reason(mode) is not None:  # 링크이거나 폴더가 아님
        return ["wiki"]
    walked = _walk_wiki(wiki)
    found = [
        "/".join(("wiki", *parts))
        for parts in (*walked.symlinks, *walked.git_names, *walked.unreadable)
    ]
    if walked.overflow:
        found.append(f"wiki/{_OVERFLOW_NAME}")
    return list(dict.fromkeys(found))


def _open_dir(path: str) -> bool:
    """실제 폴더(링크 아님)면 소유자 권한을 열고 True."""
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return False
        os.chmod(path, 0o700)
    except OSError:
        return False
    return True


def _repair_after_walk(root: str, findings: Sequence[object]) -> None:
    """`discard_changes` 전에 링크를 지우고(추적 파일은 reset이 복원) 읽을 수 없는 폴더를 연다.

    읽을 수 없는 폴더는 `clean`이 지우지 못하고 오류로 끝난다. 열 때마다 안쪽이 새로 보이므로
    더 없을 때까지(최대 `_MAX_REPAIR_ROUNDS`번) 다시 걷는다.
    """
    for finding in findings:
        if not isinstance(finding, str):
            continue
        path = os.path.join(root, finding)
        try:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                os.unlink(path)  # 링크 자체만 지운다
        except OSError:
            continue
    wiki = os.path.join(root, "wiki")
    for _ in range(_MAX_REPAIR_ROUNDS):
        mode = _lstat_mode(wiki)
        if mode is None or _wiki_reason(mode) is not None:
            return
        unreadable = _walk_wiki(wiki).unreadable
        if not any([_open_dir(os.path.join(wiki, *parts)) for parts in unreadable]):
            return


async def verify_claude_changes(git: GitClient, redact: Sequence[str] = ()) -> PathCheck:
    """마지막 커밋 이후 바뀐 경로(무시된 파일·이름 바꾸기 양쪽 포함)와 `wiki/` 안을 검사한다.

    `git status`가 보여 주지 못하는 심볼릭 링크·`.git` 이름(중첩 저장소)·읽을 수 없는 폴더는
    `wiki/`를 직접 걸어 찾는다. 위반이면 작업 사본을 되돌린다(`reset --hard` + `clean -ffdx`).
    커밋은 호출자가 한다. 스크립트의 `append_log`보다 **먼저** 불러야 한다(이 검사는 마지막 커밋
    이후만 본다). Task 9의 `run_write_step`은 이 함수만으로 링크 검사가 된다.
    git 오류·`CloneStateError`는 그대로 올라간다 — 판정할 수 없으면 커밋하지 않는 쪽이 안전하다.
    """
    paths = await changed_paths(git)
    root = os.path.abspath(git.cwd)
    try:
        findings = _walk_findings(root)
    except Exception:
        findings = [f"wiki/{_UNCHECKABLE}"]
    try:
        result = _summarize([*_violating_paths(_as_items(paths)), *findings], redact)
    except Exception:
        result = PathCheck(False, (_UNCHECKABLE,))
    if not result.ok:
        await run_to_completion(_repair_then_discard(git, root, findings))
    return result


async def _repair_then_discard(git: GitClient, root: str, findings: Sequence[object]) -> None:
    _repair_after_walk(root, findings)
    await discard_changes(git)


async def discard_work_tree(git: GitClient) -> None:
    """`wiki/`의 링크를 지우고 읽을 수 없는 폴더를 연 뒤 마지막 커밋으로 되돌린다(`clean -ffdx`).

    실패 경로(`claude_failed`·`unsettled`)가 `verify_claude_changes`와 같은 정리를 하게 한다.
    취소에 대한 보호는 없다 — 호출자가 `run_to_completion`으로 감싼다(`repair_and_discard`).
    """
    root = os.path.abspath(git.cwd)
    try:
        findings = _walk_findings(root)
    except Exception:
        findings = []
    await _repair_then_discard(git, root, findings)


async def repair_and_discard(git: GitClient) -> None:
    """`discard_work_tree`를 취소돼도 끝까지 돌린다(끝난 뒤 취소를 던진다)."""
    await run_to_completion(discard_work_tree(git))


# ---------------------------------------------------------------- 쓰기 루트 사전 검사


def _lstat_mode(path: str) -> int | None:
    try:
        return os.lstat(path).st_mode
    except FileNotFoundError:
        return None


def _wiki_reason(mode: int) -> str | None:
    if stat.S_ISLNK(mode):
        return "wiki 폴더가 심볼릭 링크입니다"
    if not stat.S_ISDIR(mode):
        return "wiki가 폴더가 아닙니다"
    return None


def _check_write_root(data_dir: Path, redact: Sequence[str] = ()) -> str | None:
    root = os.fspath(data_dir)
    if not isinstance(root, str):
        return "데이터 폴더 경로가 올바르지 않습니다"
    # 마지막 구성요소만 본다. 조상이 링크인 것(macOS `/var`)은 괜찮다.
    try:
        root_mode = os.lstat(root).st_mode
    except OSError:
        return "데이터 폴더를 읽을 수 없습니다"
    if not stat.S_ISDIR(root_mode):
        return "데이터 폴더가 폴더가 아닙니다(심볼릭 링크이거나 파일입니다)"

    wiki = os.path.join(root, "wiki")
    wiki_mode = _lstat_mode(wiki)
    if wiki_mode is None:
        try:
            os.mkdir(wiki)  # 링크를 따라가지 않는다(마지막 구성요소가 링크면 FileExistsError)
        except FileExistsError:
            pass  # 경합: 아래 lstat이 무엇이 생겼는지 본다
        except OSError:
            return "wiki 폴더를 만들 수 없습니다"
        wiki_mode = _lstat_mode(wiki)
        if wiki_mode is None:
            return "wiki 폴더를 확인할 수 없습니다"
    reason = _wiki_reason(wiki_mode)
    if reason is not None:
        return reason

    walked = _walk_wiki(wiki)
    problems: list[str] = []
    if walked.symlinks:
        names = [
            _display_path("/".join(parts), redact) for parts in walked.symlinks[:_MAX_SHOWN_LINKS]
        ]
        more = len(walked.symlinks) - len(names)
        suffix = f" 외 {more}개" if more > 0 else ""
        problems.append(f"wiki 아래에 심볼릭 링크가 있습니다: {', '.join(names)}{suffix}")
    if walked.unreadable:
        names = [
            _display_path("/".join(parts), redact) if parts else "wiki"
            for parts in walked.unreadable[:_MAX_SHOWN_LINKS]
        ]
        more = len(walked.unreadable) - len(names)
        suffix = f" 외 {more}개" if more > 0 else ""
        problems.append(f"wiki 아래에 읽을 수 없는 폴더가 있습니다: {', '.join(names)}{suffix}")
    if walked.overflow:
        problems.append("wiki 아래 항목이 너무 많아 검사하지 못했습니다")
    return "; ".join(problems) if problems else None


def check_write_root(data_dir: Path, redact: Sequence[str] = ()) -> str | None:
    """Claude가 쓰기 전에 `data_dir/wiki`가 안전한 실제 폴더인지 본다. 문제 사유 또는 `None`.

    `wiki`가 없으면 `lstat` 뒤 `os.mkdir`로 만든다(링크를 따라가지 않으므로 검사와 생성 사이에
    링크가 심겨도 바깥에 만들지 않는다). `resolve()`를 쓰지 않는다. 예외를 던지지 않는다.
    """
    try:
        return _check_write_root(data_dir, redact)
    except Exception:
        return "쓰기 루트를 확인하는 중 오류가 났습니다"
