"""위키 점검 L1~L5. AI가 쓴 위키 내용이 사람에게 닿기 전의 마지막 자동 관문이다.

- L1 비밀값, L2 깨진 상대 링크, L3 근거 누락, L4 상태 정합,
  L5 위키 폴더 형태(심볼릭 링크·숨김 항목·마크다운이 아닌 파일).
- LLM 없이 결정적으로 돈다. 페이지 내용이 적대적이어도 예외를 던지지 않고, 입력 길이에
  비례하는 시간 안에 끝난다(중첩 수량자가 든 정규식으로 긴 줄을 훑지 않는다).
- 위반 메시지에는 비밀값이 들어가지 않는다(라벨 또는 `<키워드>=…`만).
"""

import json
import os
import re
import stat
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

from team_wiki_engine.util.fs import DirCache, resolve_normalized
from team_wiki_engine.wiki.baseline import Baseline, read_baseline
from team_wiki_engine.wiki.frontmatter import parse_frontmatter
from team_wiki_engine.wiki.lint_secrets import (
    clean,
    display_name,
    looks_secret,
    safe_path,
    safe_text,
    safe_value,
    secret_findings,
    shorten,
)

__all__ = [
    "LintRule",
    "LintUsageError",
    "LintViolation",
    "format_violations",
    "lint",
    "looks_secret",  # lint_secrets.py에서 옮겼지만 import 경로는 그대로 쓸 수 있다
]

LintRule = Literal["L1", "L2", "L3", "L4", "L5"]


@dataclass(frozen=True)
class LintViolation:
    rule: LintRule
    file: str  # 데이터 저장소 루트 기준 `/` 구분 상대 경로
    message: str


class LintUsageError(Exception):
    """점검할 수 없는 입력(없는 폴더, wiki/ 없음). 위반이 아니라 사용 오류다."""


# ---------------------------------------------------------------- 상한(모두 한 곳에)
# L1 상한은 lint_secrets.py에 있다.
MAX_LINK_LABEL = 512  # 링크 라벨
MAX_LINK_TARGET = 1024  # 링크 대상
MAX_LINK_TITLE = 512  # 링크 제목
MAX_LINK_SPACE = 64  # 링크 안의 공백 연속
MAX_LINK_DEPTH = 64  # 데이터 폴더 아래 링크 대상 경로의 최대 깊이
MAX_LINK_CANDIDATES = 2000  # 페이지 하나에서 검사하는 링크 후보(`](`)의 최대 개수
MAX_PAGE_BYTES = 1_048_576  # 이보다 큰 페이지는 위반이며 L2·frontmatter 검사를 건너뛴다
BASELINE_ERROR_CHARS = 200  # baseline 오류 설명의 최대 길이
MAX_SCAN_BYTES = 8 * 1024 * 1024  # 아무리 커도 비밀값 점검을 위해 읽는 양은 이만큼까지

# ---------------------------------------------------------------- L2 링크
# 라벨 ≤ 512, 대상 ≤ 1024, 제목 ≤ 512, 공백 ≤ 64로 제한하고 소유형(+) 수량자를 쓴다.
# 이유: 길이 제한 없는 `[^\]]*`·`[^)\s>]+`는 `[`나 `](`가 반복되는 입력에서 시작 위치마다
# 끝까지 훑어 제곱 시간이 된다. 상한을 넘는 링크는 의도적으로 검사하지 않는다(통과로 본다).
# 사람이 쓰는 링크는 이 상한 안에 들어오고, 상한 밖은 병적인 입력뿐이기 때문이다.
# 페이지당 후보(`](`) 수에도 상한이 있다(MAX_LINK_CANDIDATES).
_LINK_TAIL = re.compile(  # `](`의 `(`부터 `)`까지
    rf"\(\s{{0,{MAX_LINK_SPACE}}}+<?([^)\s>]{{1,{MAX_LINK_TARGET}}}+)>?"
    rf'(?:\s{{1,{MAX_LINK_SPACE}}}+"[^"]{{0,{MAX_LINK_TITLE}}}+")?'
    rf"\s{{0,{MAX_LINK_SPACE}}}+\)"
)
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_TAG_ERROR = re.compile(r"\btag\b", re.IGNORECASE)  # PyYAML 오류 중 태그 글을 옮기는 것
_EXTERNAL_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)

# ---------------------------------------------------------------- L3·L4 규칙표
_EVIDENCE = re.compile(r"[a-z0-9-]+@[0-9a-f]{7,40}:.+")
_DEFAULT_REQUIRED = ("updated",)
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "status": ("repo", "baseline_commit", "archived", "owners", "updated"),
    "contracts": ("producer", "consumer", "medium", "status", "evidence", "updated"),
    "decisions": ("status", "decided_at", "sources", "updated"),
    "sources": ("raw", "date", "affects", "updated"),
}
_ALLOWED_STATUS: dict[str, tuple[str, ...]] = {
    "decisions": ("확정", "대체됨", "논의 중"),
    "contracts": ("현행", "대체됨"),
}
_SUPERSEDED = "대체됨"

Report = Callable[[str], None]


# ================================================================ L1·표시
def _check_secrets(text: str, report: Report, *, normalize: bool = True) -> None:
    for message in secret_findings(text, normalize=normalize):
        report(message)


def _echo(value: object) -> str:
    """frontmatter 값을 메시지에 싣기 위한 표현. 비밀값처럼 보이면 싣지 않는다."""
    if isinstance(value, str):
        return safe_value(value)
    if value is None or isinstance(value, bool | int | float):
        return clean(shorten(json.dumps(value)))
    return "목록" if isinstance(value, list) else "객체"  # 중첩 값은 내용을 싣지 않는다


# ================================================================ 경로 확인(점검 한 번 동안 공유)
class _Resolver:
    """NFC 기준 경로 확인 결과를 점검 한 번 동안 저장한다(같은 대상 링크는 한 번만 확인)."""

    def __init__(self) -> None:
        self._dirs = DirCache()
        self._real: dict[Path, Path | None] = {}
        self._regular: dict[Path, bool] = {}

    def resolve(self, path: Path) -> Path | None:
        if path not in self._real:
            self._real[path] = resolve_normalized(path, self._dirs)
        return self._real[path]

    def exists(self, path: Path) -> bool:
        return self.resolve(path) is not None

    def is_regular_file(self, path: Path) -> bool:
        """있고, 심볼릭 링크도 폴더도 아닌 일반 파일인가."""
        if path not in self._regular:
            real = self.resolve(path)
            try:
                ok = real is not None and stat.S_ISREG(os.lstat(real).st_mode)
            except (OSError, ValueError):
                ok = False
            self._regular[path] = ok
        return self._regular[path]


def _is_within(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


# ================================================================ L2
def _strip_code(text: str) -> str:
    return _INLINE_CODE.sub(" ", _FENCED_CODE.sub(" ", text))


def _find_link_targets(text: str) -> tuple[list[str], bool]:
    """`[라벨](대상 "제목")`의 대상 목록과, 후보 상한을 넘어 중단했는지.

    후보는 `](`가 나온 자리다. 각 후보에서 앞쪽 `[`(라벨 ≤ 512)를 되짚어 확인하고 뒤쪽은
    상한 있는 패턴으로 맞춰 본다. 후보 하나의 비용이 상수이고 후보 수를 제한하므로 페이지
    크기·내용과 상관없이 시간이 일정하다(예전처럼 시작 위치마다 1024자를 다시 훑지 않는다).
    """
    targets: list[str] = []
    candidates = 0
    consumed = 0  # 앞선 링크가 차지한 구간 안의 후보는 새 링크가 아니다
    pos = 0
    while (at := text.find("](", pos)) >= 0:
        pos = at + 2
        candidates += 1
        if candidates > MAX_LINK_CANDIDATES:
            return targets, True
        if at < consumed:
            continue
        # 라벨은 앞 링크가 차지한 구간 안에서 찾지 않는다
        window = text[max(consumed, at - MAX_LINK_LABEL - 1) : at]
        if window.find("[", window.rfind("]") + 1) < 0:  # 라벨 `[` 없음(라벨에는 `]`가 없다)
            continue
        tail = _LINK_TAIL.match(text, at + 1)
        if tail is None:
            continue
        targets.append(tail.group(1))
        consumed = tail.end()
    return targets, False


def _check_links(text: str, file: Path, root: Path, resolver: _Resolver, report: Report) -> None:
    targets, truncated = _find_link_targets(_strip_code(text))
    for target in targets:
        if target.startswith("#") or _EXTERNAL_SCHEME.match(target):
            continue
        # `#앵커`와 `?쿼리`를 뗀 경로만 확인하고, 메시지에도 이 경로만 싣는다
        clean_target = target.split("#", 1)[0].split("?", 1)[0]
        try:
            decoded = unquote(clean_target, errors="strict")
        except UnicodeDecodeError:
            decoded = clean_target  # 잘못된 인코딩은 적힌 그대로 확인한다
        resolved = Path(os.path.abspath(file.parent / decoded))
        depth = len(resolved.parts) - len(root.parts)
        # 지나치게 깊은 경로는 확인하지 않고 깨진 것으로 본다(경로 구성요소마다 비용이 든다)
        if (
            depth > MAX_LINK_DEPTH
            or not _is_within(root, resolved)
            or not resolver.exists(resolved)
        ):
            report(f"깨진 링크: {safe_path(clean_target, decoded)}")
    if truncated:
        report(f"링크가 너무 많아 일부만 검사했습니다 (최대 {MAX_LINK_CANDIDATES}개)")


# ================================================================ L3·L4
def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _raw_file_exists(raw_dir: Path, name: str, resolver: _Resolver) -> bool:
    """`raw/` 아래(자기 자신 제외)에 `name`(NFC 기준)의 일반 파일이 있는가."""
    target = Path(os.path.abspath(raw_dir / name))
    return target != raw_dir and _is_within(raw_dir, target) and resolver.is_regular_file(target)


def _check_evidence(evidence: Any, report: Report) -> None:
    if not isinstance(evidence, list) or not evidence:
        report("evidence가 비었습니다")
        return
    for item in evidence:
        if not isinstance(item, str) or _EVIDENCE.fullmatch(item) is None:
            report(f"evidence 형식 오류(저장소@커밋:경로): {_echo(item)}")


def _check_decision_sources(
    sources: Any, raw_dir: Path, resolver: _Resolver, report: Report
) -> None:
    if not isinstance(sources, list):
        report("sources는 원본 파일 이름의 목록이어야 합니다")
    elif not sources:
        report("sources가 비었습니다")
    else:
        for item in sources:
            if not isinstance(item, str):
                report(f"sources 항목은 파일 이름이어야 합니다: {_echo(item)}")
            elif not _raw_file_exists(raw_dir, item, resolver):
                report(f"근거 원본이 없습니다: {_echo(item)}")


def _check_superseded_by(target: str, file: Path, resolver: _Resolver, report: Report) -> None:
    """`superseded_by`는 같은 폴더의 다른 일반 `.md` 파일이어야 한다(`.md`는 생략 가능)."""
    name = target if target.endswith(".md") else f"{target}.md"
    resolved = Path(os.path.abspath(file.parent / name))
    if resolved.parent != file.parent or not resolver.is_regular_file(resolved):
        report(f"superseded_by 대상이 없습니다: {_echo(target)}")
        return
    real = resolver.resolve(resolved)
    try:
        same = real is not None and os.path.samefile(real, file)
    except OSError:
        same = False
    if same:
        report("superseded_by가 자기 자신을 가리킵니다")


def _check_status(
    folder: str, data: dict[str, Any], file: Path, resolver: _Resolver, report: Report
) -> None:
    allowed = _ALLOWED_STATUS.get(folder, ())
    raw = data.get("status")
    status = unicodedata.normalize("NFC", raw) if isinstance(raw, str) else raw
    if "status" in data and (not isinstance(status, str) or status not in allowed):
        # 값은 싣지 않는다: 잘못 붙여 넣은 비밀값일 수 있고, 허용 목록만으로 고칠 수 있다
        report(f"status 값이 허용 목록 밖입니다 (허용: {', '.join(allowed)})")
    if status != _SUPERSEDED:
        return
    target = data.get("superseded_by")
    if not isinstance(target, str) or target.strip() == "":
        report("대체됨인데 superseded_by가 없습니다")
        return
    _check_superseded_by(target, file, resolver, report)


def _check_front_fields(
    folder: str,
    data: dict[str, Any],
    file: Path,
    raw_dir: Path,
    resolver: _Resolver,
    l3: Report,
    l4: Report,
) -> None:
    for key in _REQUIRED_FIELDS.get(folder, _DEFAULT_REQUIRED):
        if _is_missing(data.get(key)):
            l3(f"필수 필드 누락: {key}")
    if folder == "contracts" and not _is_missing(data.get("evidence")):
        _check_evidence(data["evidence"], l3)
    if folder == "sources" and not _is_missing(data.get("raw")):
        raw = data["raw"]
        if not isinstance(raw, str):
            l3(f"raw는 원본 파일 이름이어야 합니다: {_echo(raw)}")
        elif not _raw_file_exists(raw_dir, raw, resolver):
            l3(f"raw 파일이 없습니다: {_echo(raw)}")
    if folder == "decisions" and not _is_missing(data.get("sources")):
        _check_decision_sources(data["sources"], raw_dir, resolver, l3)
    if folder in _ALLOWED_STATUS:
        _check_status(folder, data, file, resolver, l4)


# ================================================================ L5 위키 폴더 형태
def _walk_wiki(
    wiki_dir: Path, display: Callable[[str], str]
) -> tuple[list[LintViolation], list[Path]]:
    """`wiki/`를 심볼릭 링크를 따라가지 않고 한 번 훑는다.

    점검 대상 페이지(숨김이 아닌 폴더 안의 소문자 `.md` 일반 파일)와, 그 밖의 모든 항목에 대한
    L5 위반(심볼릭 링크, 숨김 항목, 마크다운이 아닌 파일, 읽을 수 없는 폴더)을 돌려준다.
    점검하지 않는 항목이 조용히 통과하지 않게 하는 것이 목적이다. 숨김 폴더 안은 들어가지 않는다.
    `.DS_Store` 같은 숨김 파일도 위반이므로 지워야 한다(fail-closed).
    """
    problems: list[tuple[str, str]] = []  # (정렬 키 경로, 메시지)
    pages: list[str] = []
    pending = [str(wiki_dir)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            problems.append((current, "위키 폴더를 읽을 수 없습니다"))
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    problems.append((entry.path, "위키 폴더에는 심볼릭 링크를 둘 수 없습니다"))
                elif entry.name.startswith("."):
                    problems.append((entry.path, "위키 폴더에는 숨김 항목을 둘 수 없습니다"))
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif entry.name.endswith(".md") and entry.is_file(follow_symlinks=False):
                    pages.append(entry.path)
                else:
                    problems.append((entry.path, "위키 폴더에는 마크다운 페이지만 둘 수 있습니다"))
            except OSError:
                problems.append((entry.path, "위키 폴더를 읽을 수 없습니다"))
    violations = [
        LintViolation("L5", display(path), f"{message}: {display(path)}")
        for path, message in sorted(problems)
    ]
    return violations, [Path(p) for p in sorted(pages)]


# ================================================================ lint
class _FileReports:
    """한 파일의 위반을 `out`에 모은다. 같은 규칙의 같은 지적은 파일마다 한 번만 싣는다."""

    def __init__(self, out: list[LintViolation], name: str) -> None:
        self._out = out
        self._name = name
        self._seen: set[tuple[LintRule, str]] = set()

    def __call__(self, rule: LintRule) -> Report:
        return partial(self._add, rule)

    def _add(self, rule: LintRule, message: str) -> None:
        if (rule, message) not in self._seen:
            self._seen.add((rule, message))
            self._out.append(LintViolation(rule, self._name, message))


def _describe_parse_error(error: str) -> str:
    """frontmatter 오류를 메시지용으로 바꾼다. 태그 오류는 태그 글(비밀번호일 수 있다)은 뺀다."""
    if _TAG_ERROR.search(error):
        return "frontmatter YAML 오류: 태그(!) 구문은 쓸 수 없습니다"
    return safe_text(error)


def _read_page(file: Path) -> tuple[bytes, int]:
    """페이지의 앞 `MAX_SCAN_BYTES`와 전체 크기. 아무리 커도 그 이상은 읽지 않는다."""
    with file.open("rb") as f:
        return f.read(MAX_SCAN_BYTES), os.fstat(f.fileno()).st_size


def _lint_page(
    file: Path, root: Path, wiki_dir: Path, resolver: _Resolver, reports: _FileReports
) -> tuple[str, object] | None:
    """페이지 하나를 점검한다. 현황 페이지면 `(repo, archived)`를 돌려준다."""
    try:
        payload, size = _read_page(file)
    except OSError:
        reports("L3")("파일을 읽을 수 없습니다")
        return None
    if size > MAX_PAGE_BYTES:
        note = f"페이지가 너무 큽니다 (크기 {size:,}바이트, 최대 {MAX_PAGE_BYTES:,}바이트)"
        if size > MAX_SCAN_BYTES:
            note += f". 앞 {MAX_SCAN_BYTES:,}바이트만 비밀값을 점검했습니다"
        reports("L3")(note)
        _check_secrets(payload.decode("utf-8", errors="replace"), reports("L1"), normalize=False)
        return None  # 링크·frontmatter 검사는 건너뛴다
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.decode("utf-8", errors="replace")
        reports("L3")("UTF-8 텍스트가 아닙니다")
    _check_secrets(text, reports("L1"))
    _check_links(text, file, root, resolver, reports("L2"))

    parsed = parse_frontmatter(text)
    if parsed.error is not None:
        reports("L3")(_describe_parse_error(parsed.error))
        return None
    if parsed.data is None:
        reports("L3")("frontmatter가 없습니다")
        return None
    parts = file.relative_to(wiki_dir).parts
    folder = parts[0] if len(parts) > 1 else ""
    _check_front_fields(
        folder, parsed.data, file, root / "raw", resolver, reports("L3"), reports("L4")
    )
    repo = parsed.data.get("repo")
    if folder == "status" and isinstance(repo, str):
        return repo, parsed.data.get("archived")
    return None


def _require_wiki_dir(root: Path, wiki_dir: Path) -> None:
    if not os.path.isdir(root):
        raise LintUsageError(f"데이터 폴더를 읽을 수 없습니다: {root}")
    if not os.path.isdir(wiki_dir):
        raise LintUsageError(f"wiki 폴더가 없습니다: {wiki_dir}")
    if os.path.islink(wiki_dir):  # 링크 너머의 내용은 점검 범위 밖이라 통과로 보이면 안 된다
        raise LintUsageError(f"wiki 폴더가 심볼릭 링크입니다: {wiki_dir}")
    try:
        with os.scandir(wiki_dir):
            pass
    except OSError as exc:
        raise LintUsageError(f"wiki 폴더를 읽을 수 없습니다: {wiki_dir}") from exc


def lint(data_dir: Path) -> list[LintViolation]:
    """데이터 저장소의 `wiki/`를 점검한다. 폴더가 없거나 읽을 수 없으면 `LintUsageError`."""
    root = Path(os.path.abspath(data_dir))
    wiki_dir = root / "wiki"
    _require_wiki_dir(root, wiki_dir)
    resolver = _Resolver()
    out: list[LintViolation] = []

    def rel(path: Path) -> str:  # 표시용 이름: 비밀값 같은 이름은 가리고 제어 문자는 이스케이프한다
        return "/".join(display_name(part) for part in path.relative_to(root).parts)

    baseline: Baseline | None = None
    try:
        baseline = read_baseline(root)
    except (ValueError, OSError) as exc:
        out.append(
            LintViolation(
                "L4",
                "baseline.json",
                f"baseline.json을 읽을 수 없습니다: {safe_text(str(exc), BASELINE_ERROR_CHARS)}",
            )
        )

    shape, pages = _walk_wiki(wiki_dir, lambda path: rel(Path(path)))
    out.extend(shape)

    status_pages: dict[str, tuple[Path, object]] = {}
    for file in pages:
        status = _lint_page(file, root, wiki_dir, resolver, _FileReports(out, rel(file)))
        if status is not None:
            status_pages[status[0]] = (file, status[1])

    if baseline is not None:
        for repo_name, repo_base in baseline.repos.items():
            page = status_pages.get(repo_name)
            if not repo_base.archived and page is None:
                shown = safe_text(repo_name)
                out.append(
                    LintViolation(
                        "L4", "baseline.json", f"현황 페이지가 없습니다: wiki/status/{shown}.md"
                    )
                )
            if page is not None and page[1] is not repo_base.archived:  # `0`/`1`은 불리언이 아니다
                out.append(
                    LintViolation(
                        "L4",
                        rel(page[0]),
                        "archived 값이 baseline.json과 다릅니다"
                        f"(baseline: {str(repo_base.archived).lower()})",
                    )
                )
    return out


def format_violations(violations: Sequence[LintViolation]) -> str:
    if not violations:
        return "점검 통과"
    lines = [f"[{v.rule}] {v.file}: {v.message}" for v in violations]
    return "\n".join([*lines, f"위반 {len(violations)}건"])
