"""Claude 하위 에이전트가 읽고 쓸 수 있는 경로를 판정한다. 보안 경계다.

`decide()`가 유일한 판정 지점이다. 항상 `Allow`나 `Deny`를 돌려주고 예외를 던지지 않는다.
이상한 입력, 판정 도중의 파일시스템 오류는 모두 `Deny`로 닫는다(fail-closed).

SDK는 경로를 스스로 펼친 뒤(앞뒤 공백 제거, `~` → 홈) 도구를 실행하지만 Grep/Glob 같은 도구는
훅에 원본 문자열을 넘긴다. 그래서 `~`로 시작하거나 앞뒤에 공백이 있는 경로는 해석하지 않고
거부한다. Glob은 CLI가 한 검색 폴더에서 ripgrep을 돌리고 패턴은 그 아래를 거르는 필터이므로
상대 패턴의 `..`·중괄호 검사는 필요 이상으로 거부할 수 있는 이중 방어이고, 판정은 검색 루트
(`path`, 없으면 cwd)와 패턴의 고정 접두 경로가 모두 허용 루트 안일 것을 요구한다.
절대 패턴은 CLI가 실제로 훑는 폴더가 중요하다. 우리의 고정 접두 경로, CLI의 기준 폴더
(`cli_glob_base`), 그 기준 폴더에 CLI의 trim·normalize를 적용한 폴더(`cli_glob_search_dir`),
글롭 문자가 없을 때는 리터럴 경로 자체까지 모두 허용 루트 안이어야 한다.
경로 문자열은 NFC·UTF-8이어야 하고(CLI가 NFC로 바꾸므로) 2048자를 넘지 못한다.
"""

import errno
import os
import posixpath
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

READ_TOOLS: frozenset[str] = frozenset({"Read", "Grep", "Glob"})
WRITE_TOOLS: frozenset[str] = frozenset({"Edit", "Write"})

MAX_INPUT_LENGTH = 2048
MAX_BRACE_EXPANSIONS = 256
MAX_BRACE_DEPTH = 16

_FILE_PATH_TOOLS = frozenset({"Read", "Edit", "Write"})
_GLOB_CHARS = frozenset("*?[]{}")
_CLI_GLOB_CHARS = frozenset("*?[{")
# JS `String.prototype.trim()`이 자르는 문자. Python의 `str.strip()`은 U+FEFF를 자르지 않는다.
_JS_WHITESPACE = "".join(
    [
        " \t\n\v\f\r",
        *(chr(c) for c in (0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029)),
        *(chr(c) for c in (0x202F, 0x205F, 0x3000, 0xFEFF)),
    ]
)
_PIECE_SPLIT = re.compile(r"[\\/]")
_PIECE_SPLIT_WIDE = re.compile(r"[\\/,{}]")
# Python의 str.strip()이 자르지 않지만 JS의 trim()은 자르는 문자(SDK는 JS로 경로를 다듬는다).
_JS_TRIM_ONLY = frozenset({chr(0xFEFF)})


# ---------------------------------------------------------------- 결과 타입


@dataclass(frozen=True)
class Allow:
    """허용. `if d.allow:` 로 `Deny`와 구분된다."""

    allow: Literal[True] = True


@dataclass(frozen=True)
class Deny:
    """거부. `reason`은 사람이 읽을 한국어 사유다."""

    reason: str
    allow: Literal[False] = False


Decision = Allow | Deny


# ---------------------------------------------------------------- 컨텍스트


def _require_absolute(value: object, label: str) -> None:
    if not isinstance(value, Path):
        raise ValueError(f"{label}는 Path여야 합니다: {value!r}")
    if "\x00" in str(value):
        raise ValueError(f"{label}에 NUL 문자가 들어 있습니다")
    if not value.is_absolute():
        # Path("")도 Path(".")가 되어 여기서 걸린다. 상대 경로는 프로세스 cwd 기준이 되어 위험하다.
        raise ValueError(f"{label}는 비어 있지 않은 절대 경로여야 합니다: {str(value)!r}")


@dataclass(frozen=True)
class GuardContext:
    """판정 기준. `cwd`와 모든 루트는 절대 경로여야 한다(아니면 `ValueError`)."""

    cwd: Path
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]

    def __post_init__(self) -> None:
        _require_absolute(self.cwd, "cwd")
        for label in ("read_roots", "write_roots"):
            raw = getattr(self, label)
            try:
                roots = tuple(raw)
            except TypeError as exc:
                raise ValueError(f"{label}는 Path 목록이어야 합니다: {raw!r}") from exc
            for index, root in enumerate(roots):
                _require_absolute(root, f"{label}[{index}]")
            object.__setattr__(self, label, roots)


# ---------------------------------------------------------------- 정규화


def canonicalize(p: str | Path) -> Path:
    """심볼릭 링크를 모두 해소한 경로. 없는 부분은 해소 가능한 데까지 해소하고 나머지를 붙인다.

    깨진 링크는 가리키는 곳으로 해소한다(링크를 새 파일 이름처럼 보지 않는다). 상대 경로는
    프로세스의 cwd 기준이므로 호출자가 절대 경로를 넘긴다. 링크 순환이면 `OSError`를 던진다.
    """
    resolved = Path(os.path.realpath(p))
    try:
        mode = os.lstat(resolved).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return resolved
    if stat.S_ISLNK(mode):
        # 완전히 해소된 경로가 링크로 남았다면 순환이다.
        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(resolved))
    return resolved


def is_inside(p: Path, roots: Iterable[Path]) -> bool:
    """`p`가 `roots` 중 하나(자신 포함) 안에 있는지. 양쪽 모두 링크를 해소해 비교한다."""
    try:
        target = canonicalize(p)
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            if target.is_relative_to(canonicalize(root)):
                return True
        except (OSError, ValueError):
            continue
    return False


# ---------------------------------------------------------------- Glob 패턴


def glob_static_prefix(pattern: str) -> str:
    """글롭 문자(`*?[]{}`)가 처음 나오는 구간 앞까지의 고정 경로. 비면 `/`(절대) 또는 `.`."""
    fixed: list[str] = []
    for part in _PIECE_SPLIT.split(pattern):
        if any(ch in _GLOB_CHARS for ch in part):
            break
        fixed.append(part)
    joined = "/".join(fixed)
    if joined == "":
        return "/" if pattern.startswith(("/", "\\")) else "."
    return joined


def cli_glob_base(pattern: str) -> str:
    """번들 CLI가 *절대* Glob 패턴에서 뽑는 기준 폴더(`Ano`). trim·normalize 이전의 원본이다.

    CLI 바이너리에서 확인한 규칙:
    - 글롭 문자(`*`, `?`, `[`, `{`)가 있으면 그중 처음 나오는 곳에서 자르고, 그 앞의 마지막 `/`
      위치 `g`에서 `pattern[:g]`를 쓴다. 슬래시를 따로 지우지 않아 `g` 앞의 슬래시 연속은 남는다
      (`<data>/.. //*`의 기준 폴더는 `<data>/.. /`). `]`·`}`·백슬래시에서는 멈추지 않는다.
    - 글롭 문자가 없으면 패턴 전체가 리터럴 필터이고 기준 폴더는 posix `path.dirname`이다.
      끝의 `/`는 모두 무시하고 마지막 구성요소 앞의 슬래시 *하나*만 지운다(`/a//b` -> `/a/`).
    그 뒤에 `Ye`가 trim, normalize, NFC를 한다(순서는 자르기, trim, normalize). 이 단계는
    `cli_glob_search_dir`가 한다.
    """
    cut = next((i for i, ch in enumerate(pattern) if ch in _CLI_GLOB_CHARS), None)
    if cut is None:
        literal = pattern.rstrip("/") or "/"
        last_slash = literal.rfind("/")
        if last_slash == -1:
            return "."
        return literal[:last_slash] or "/"
    last_slash = pattern.rfind("/", 0, cut)
    if last_slash == -1:
        return "."
    return pattern[:last_slash] or "/"


def cli_glob_search_dir(pattern: str) -> str:
    """CLI가 ripgrep을 돌리는 폴더(`Ye(cli_glob_base)`)의 보수적 근사: JS trim, normalize, NFC.

    끝 공백이 붙은 `.. `는 trim 뒤 `..`가 되어 부모 폴더로 접히고, `//`·`.`도 접힌다. Node의
    `path.normalize`는 맨 앞의 `//`도 `/`로 접는다(posixpath는 그대로 둔다).
    실제 CLI는 기준 폴더의 슬래시 연속을 지우지 않고 trim하므로(`name<공백>//*`의 공백은 안 잘린다)
    여기서는 trim 전에 끝 슬래시를 모두 지운다. 그러면 공백이 잘려 더 바깥 폴더로 접힐 수 있어
    실제보다 같거나 넓게 본다. 안전한 쪽의 과잉 근사라 `<공백>//`로 끝나는 기준 폴더 같은
    드문 이름은 더 거부될 수 있다. 원본 기준 폴더(`cli_glob_base`)도 따로 검사하므로
    실제 값은 빠지지 않는다.
    """
    base = cli_glob_base(pattern).rstrip("/") or "/"
    base = unicodedata.normalize("NFC", base.strip(_JS_WHITESPACE))
    normalized = posixpath.normpath(base)
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


class _Reject(Exception):  # noqa: N818 - 판정 흐름 제어용 내부 예외
    """판정을 거부로 끝낸다. 메시지가 거부 사유다."""


def _brace_pairs(pattern: str) -> dict[int, int]:
    """짝이 맞는 `{`→`}` 위치. 짝 없는 괄호는 글자로 본다. 선형 시간."""
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for index, ch in enumerate(pattern):
        if ch == "{":
            stack.append(index)
            if len(stack) > MAX_BRACE_DEPTH:
                raise _Reject(f"Glob 패턴의 중괄호가 너무 깊게 중첩되어 있습니다: {_show(pattern)}")
        elif ch == "}" and stack:
            pairs[stack.pop()] = index
    return pairs


def _alternatives(pattern: str, pairs: dict[int, int], start: int) -> list[tuple[int, int]]:
    """`{`(start) 그룹을 최상위 쉼표로 나눈 대안들의 [시작, 끝) 범위."""
    end = pairs[start]
    ranges: list[tuple[int, int]] = []
    piece_start = start + 1
    i = start + 1
    while i < end:
        if pattern[i] == "{" and i in pairs:
            i = pairs[i] + 1
            continue
        if pattern[i] == ",":
            ranges.append((piece_start, i))
            piece_start = i + 1
        i += 1
    ranges.append((piece_start, end))
    return ranges


def _count_variants(pattern: str, pairs: dict[int, int], lo: int, hi: int) -> int:
    """`[lo, hi)`를 중괄호 확장했을 때의 개수(상한을 넘으면 상한+1로 멈춘다)."""
    cap = MAX_BRACE_EXPANSIONS + 1
    total = 1
    i = lo
    while i < hi:
        if pattern[i] == "{" and i in pairs:
            group = sum(
                _count_variants(pattern, pairs, a, b) for a, b in _alternatives(pattern, pairs, i)
            )
            total = min(total * group, cap)
            i = pairs[i] + 1
        else:
            i += 1
    return total


def _expand(pattern: str, pairs: dict[int, int], lo: int, hi: int) -> list[str]:
    """`[lo, hi)`의 중괄호 확장 결과. 개수가 상한 이하임을 확인한 뒤에만 부른다."""
    variants = [""]
    literal: list[str] = []
    i = lo
    while i < hi:
        if pattern[i] == "{" and i in pairs:
            text = "".join(literal)
            literal.clear()
            alts = [
                v
                for a, b in _alternatives(pattern, pairs, i)
                for v in _expand(pattern, pairs, a, b)
            ]
            variants = [v + text + alt for v in variants for alt in alts]
            i = pairs[i] + 1
        else:
            literal.append(pattern[i])
            i += 1
    tail = "".join(literal)
    return [v + tail for v in variants]


def _check_glob_pattern(pattern: str) -> None:
    """Glob 패턴이 `..` 구간을 만들 수 있으면(중괄호 확장 포함) `_Reject`를 던진다."""
    # 확장 전 원문도 `{},`까지 쪼개어 본다: 짝이 안 맞는 중괄호를 달리 해석하는 엔진에 대비한다.
    if ".." in _PIECE_SPLIT_WIDE.split(pattern):
        raise _Reject(f"Glob 패턴에는 '..'를 쓸 수 없습니다: {_show(pattern)}")
    pairs = _brace_pairs(pattern)
    if not pairs:
        return
    if _count_variants(pattern, pairs, 0, len(pattern)) > MAX_BRACE_EXPANSIONS:
        raise _Reject(f"Glob 패턴의 중괄호 확장이 너무 많습니다: {_show(pattern)}")
    if any(".." in _PIECE_SPLIT.split(v) for v in _expand(pattern, pairs, 0, len(pattern))):
        raise _Reject(f"Glob 패턴에는 '..'를 쓸 수 없습니다: {_show(pattern)}")


# ---------------------------------------------------------------- 경로 해석


def _show(value: object, limit: int = 120) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _check_path_string(raw: str, label: str) -> None:
    """SDK의 `expandPath`와 어긋날 수 있는 문자열을 해석하기 전에 거부한다."""
    if len(raw) > MAX_INPUT_LENGTH:
        raise _Reject(f"{label}가 너무 깁니다(최대 {MAX_INPUT_LENGTH}자)")
    if "\x00" in raw:
        raise _Reject(f"{label}에 NUL 문자를 쓸 수 없습니다")
    # CLI는 경로를 NFC로 정규화하고 UTF-8로 쓴다. 가드가 정규화 전 철자를 보면 존재하지 않는 이름을
    # 검사하게 되어(리눅스는 NFC/NFD를 다른 이름으로 둔다) 읽기·링크 검사를 건너뛸 수 있다.
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise _Reject(
            f"{label}가 올바른 UTF-8 문자열이 아니라 NFC 정규화를 할 수 없습니다: {_show(raw)}"
        ) from None
    if not unicodedata.is_normalized("NFC", raw):
        raise _Reject(f"{label}는 NFC로 정규화된 문자열이어야 합니다: {_show(raw)}")
    if raw and (
        raw != raw.strip()
        or raw[0] in _JS_TRIM_ONLY
        or raw[-1] in _JS_TRIM_ONLY
        or raw.startswith("~")
    ):
        raise _Reject(f"경로에 '~'나 앞뒤 공백을 쓸 수 없습니다: {_show(raw)}")


def _file_path(tool_input: Mapping[str, object]) -> str:
    raw = tool_input.get("file_path")
    if not isinstance(raw, str):
        raise _Reject("file_path가 없거나 문자열이 아닙니다")
    _check_path_string(raw, "file_path")
    if raw.strip() == "":
        raise _Reject("file_path가 비어 있습니다")
    return raw


def _search_path(tool_input: Mapping[str, object], cwd: Path) -> Path:
    """Grep/Glob의 검색 폴더. `path`가 없거나 비어 있으면 cwd."""
    if "path" not in tool_input:
        return cwd
    raw = tool_input["path"]
    if not isinstance(raw, str):
        raise _Reject("path가 문자열이 아닙니다")
    _check_path_string(raw, "path")
    return cwd / raw if raw else cwd


def _glob_pattern(tool_input: Mapping[str, object]) -> str | None:
    if "pattern" not in tool_input:
        return None
    pattern = tool_input["pattern"]
    if not isinstance(pattern, str):
        raise _Reject("pattern이 문자열이 아닙니다")
    if pattern == "":
        return None
    _check_path_string(pattern, "pattern")
    _check_glob_pattern(pattern)
    return pattern


@dataclass(frozen=True)
class _Resolved:
    lexical: Path  # 링크를 해소하기 전, `..`를 문자열로 접은 절대 경로
    canonical: tuple[Path, ...]  # 링크를 해소한 결과. `..`가 있으면 OS식 해석도 함께 든다


def _resolve(joined: Path) -> _Resolved:
    # SDK는 `..`를 문자열로 접고, OS는 링크를 따라간 뒤 접는다. 둘 다 안에 있어야 한다.
    lexical = Path(os.path.normpath(joined))
    canonical = [canonicalize(lexical)]
    if ".." in joined.parts:
        physical = canonicalize(joined)
        if physical != canonical[0]:
            canonical.append(physical)
    return _Resolved(lexical, tuple(canonical))


def _resolve_tool_input(tool_name: object, tool_input: object, cwd: Path) -> _Resolved:
    if not isinstance(tool_name, str) or tool_name not in READ_TOOLS | WRITE_TOOLS:
        shown = _show(tool_name) if not isinstance(tool_name, str) else _show(tool_name, 60)
        raise _Reject(f"허용되지 않은 도구입니다: {shown}")
    if not isinstance(tool_input, Mapping):
        raise _Reject(f"{tool_name}: 도구 입력이 올바르지 않습니다")

    if tool_name in _FILE_PATH_TOOLS:
        raw = _file_path(tool_input)
        if tool_name in WRITE_TOOLS and ".." in raw.split("/"):
            # 쓰기는 링크를 따라간 뒤 접는 해석과 문자열로 접는 해석이 갈리는 경로를 아예 막는다.
            raise _Reject(f"쓰기 경로에는 '..'를 쓸 수 없습니다: {_show(raw)}")
        return _resolve(cwd / raw)

    base = _search_path(tool_input, cwd)
    if tool_name != "Glob":
        return _resolve(base)
    pattern = _glob_pattern(tool_input)
    if pattern is None:
        return _resolve(base)
    resolved = _resolve(base / glob_static_prefix(pattern))
    # CLI는 검색 루트(`path`, 없으면 cwd)에서 ripgrep을 돌린다. 패턴이 안쪽을 가리켜도(예: 밖
    # 폴더에 놓인 루트 안 링크) 루트가 밖이면 그 폴더를 통째로 훑으므로 검색 루트도 안이어야 한다.
    candidates: list[Path] = [base]
    if pattern.startswith("/"):
        # 절대 패턴은 CLI가 훑는 폴더도 본다. 다듬기 전 기준 폴더, 다듬은 폴더, 우리의 고정 접두
        # 경로가 모두 안에 있어야 하고, 글롭 문자가 없으면 리터럴 자체도 본다.
        candidates += [Path(cli_glob_base(pattern)), Path(cli_glob_search_dir(pattern))]
        if not any(ch in _CLI_GLOB_CHARS for ch in pattern):
            candidates.append(Path(pattern.rstrip("/") or "/"))
    extra: list[Path] = []
    for candidate in candidates:
        extra.extend(
            c for c in _resolve(candidate).canonical if c not in (*resolved.canonical, *extra)
        )
    return _Resolved(resolved.lexical, (*resolved.canonical, *extra))


def resolve_tool_path(tool_name: str, tool_input: Mapping[str, object], cwd: Path) -> Path | None:
    """도구가 건드릴 경로를 링크를 해소한 절대 경로로 돌려준다. 판정할 수 없으면 `None`.

    Read/Edit/Write는 `file_path`, Grep/Glob은 `path`(없으면 `cwd`)이고 Glob은 패턴의 고정
    접두 경로까지 붙인다. `~`·앞뒤 공백·NUL·긴 경로·`..`를 만드는 Glob 패턴은 `None`이다.
    """
    try:
        return _resolve_tool_input(tool_name, tool_input, cwd).canonical[0]
    except (_Reject, OSError, ValueError):
        return None


# ---------------------------------------------------------------- 쓰기 링크 검사


def _writes_through_symlink(lexical: Path, root: Path) -> bool:
    """루트(링크 해소된 경로) 아래로 내려가는 구성요소 중 심볼릭 링크가 있으면 True.

    루트에 이르기까지(예: macOS `/var`, 링크로 넘어온 루트)의 링크는 따라가되, 루트 안에 들어선
    뒤 만나는 링크는 깨진 것이든 아니든 모두 막는다. 읽을 수 없는 구성요소도 막는다.
    """
    physical = Path(lexical.anchor)
    inside = physical.is_relative_to(root)
    for name in lexical.parts[1:]:
        step = physical / name
        try:
            mode = os.lstat(step).st_mode
        except FileNotFoundError:
            return False  # 여기부터는 아직 없는 경로라 링크가 있을 수 없다
        except OSError:
            return True
        if stat.S_ISLNK(mode):
            if inside:
                return True
            step = Path(os.path.realpath(step))
        physical = step
        inside = inside or physical.is_relative_to(root)
    return False


def _may_write(resolved: _Resolved, write_roots: Iterable[Path]) -> bool:
    for root in write_roots:
        try:
            canonical_root = canonicalize(root)
        except (OSError, ValueError):
            continue
        if all(c.is_relative_to(canonical_root) for c in resolved.canonical) and (
            not _writes_through_symlink(resolved.lexical, canonical_root)
        ):
            return True
    return False


# ---------------------------------------------------------------- 판정


def _decide(tool_name: str, tool_input: Mapping[str, object], ctx: GuardContext) -> Decision:
    resolved = _resolve_tool_input(tool_name, tool_input, ctx.cwd)
    is_write = tool_name in WRITE_TOOLS

    names = {resolved.lexical.name, *(c.name for c in resolved.canonical)}
    if any(name.casefold() == ".env" for name in names):
        return Deny(".env 파일은 읽거나 쓸 수 없습니다")

    if is_write:
        parts = (*resolved.lexical.parts, *(p for c in resolved.canonical for p in c.parts))
        if any(part.casefold() == ".git" for part in parts):
            return Deny(f".git 안에는 쓸 수 없습니다: {resolved.lexical}")
        if _may_write(resolved, ctx.write_roots):
            return Allow()
        return Deny(f"쓰기가 허용되지 않은 경로입니다(폴더 밖이거나 링크 통과): {resolved.lexical}")

    read_roots = [ctx.cwd, *ctx.read_roots]
    for target in resolved.canonical:
        if not is_inside(target, read_roots):
            return Deny(f"허용된 폴더 밖입니다: {target}")
    return Allow()


def decide(tool_name: str, tool_input: Mapping[str, object], ctx: GuardContext) -> Decision:
    """도구 호출이 건드릴 경로를 허용할지 판정한다. 예외를 던지지 않고 이상하면 거부한다."""
    try:
        return _decide(tool_name, tool_input, ctx)
    except _Reject as reject:
        return Deny(str(reject))
    except Exception as exc:  # 보안 경계라 어떤 오류든 거부로 닫는다
        return Deny(f"경로를 확인할 수 없어 거부합니다({type(exc).__name__})")
