"""PR 본문 작성과 전달.

PR 본문은 AI가 쓴 글과 공격자가 영향을 줄 수 있는 글(커밋 제목, GitHub PR 제목, 파일 이름)이
팀원에게 공개되는 유일한 곳이다(계획 G13). 그래서 본문은 이렇게 만든다.

- 줄 단위 항목은 `clean()`으로 제어·서식·방향 문자를 눈에 보이게 바꿔 한 줄로 만든다
  (줄·절 위조 금지). 여러 줄 글(문서 분석, 확신이 낮은 부분)은 줄바꿈만 남기고
  줄마다 같은 처리를 한다.
- 점검 위반은 코드 펜스에 넣되 펜스를 내용 속 가장 긴 백틱 연속보다 길게 한다(`fence`).
- 모델이 쓴 글 속의 열린 펜스는 글 끝에서 닫는다(뒤 절 전체가 코드로 렌더링되는 것을 막는다).
  목록 항목·인용 안의 펜스는 줄 앞 표지를 같이 맞춰 닫는다(완전한 마크다운 파서가 아니라 그럴듯한
  근사다 — 남는 한계는 report에 적었다).
- **완성된 본문 전체**에 `neutralize_mentions`를 건다. U+200B가 글자 수를 늘리므로 줄임 판단과 최종
  길이 검사는 모두 무력화 **뒤** 길이로 한다. 줄이다가 펜스 안에서 끊기면 펜스를 닫는다.

문서 분석은 줄임 단계마다 개수도 줄인다(가장 느슨한 단계 1000개, 이후 목록 상한을 따르되 가장
빡빡한 단계에서도 3개의 제목은 남기고 나머지는 "… 외 N건"). `GitHubPrPort.warnings`(라벨 달기 실패
같은 경고)는 이 모듈이 읽지 않는다: 호출자(Task 14·15의 단계)가 `PrBodyInput.warnings`나 운영 보고에
실어야 한다.

전달(`deliver`)은 강제 push를 하지 않는다. push는 `run_to_completion` 안에서 돈다: 취소가 와도
git을 죽이지 않고 끝까지 둔다. 로컬 전달(`file` 원격)에서는 push를 받는 `receive-pack`도
같은 프로세스 그룹이라 도중에 죽이면 bare 저장소에 `refs/heads/*.lock`이 남을 수 있기
때문이다. 대기는 `GitClient`의 시간 제한을 넘지 못한다. 취소가 push 뒤에 처리되면 PR은 열지
않는다(브랜치만 올라가 있고 다음 실행의 `find_open`·작업 브랜치 재설정이 정리한다).
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

from team_wiki_engine.git.client import GitClient
from team_wiki_engine.git.ops import is_safe_branch, push, rev_parse
from team_wiki_engine.sync.pr_port import (
    SYNC_LABELS,
    OpenPrRequest,
    PrPort,
    read_tracking,
    write_tracking,
)
from team_wiki_engine.sync.types import SyncKind
from team_wiki_engine.util.aio import run_to_completion
from team_wiki_engine.wiki.lint import LintViolation, format_violations
from team_wiki_engine.wiki.lint_secrets import clean, safe_text

ZWSP: Final = "\u200b"

MAX_TITLE_CHARS: Final = 300
DEFAULT_TITLE: Final = "위키 동기화"
_ITEM_CHARS: Final = 500
"""목록 항목 하나의 길이 상한(제어 문자 이스케이프 전 기준)."""
_ANALYSIS_TITLE_CHARS: Final = 200
_RUN_ID_CHARS: Final = 100
_MAX_LISTED: Final = 1000
"""목록 하나에서 가장 많이 싣는 항목 수(가장 느슨한 줄임 단계에서도)."""
_ANALYSIS_FLOOR: Final = 3
"""가장 빡빡한 단계에서도 제목은 남기는 문서 분석 수."""
_LOW_CONFIDENCE_CHARS: Final = 1500
_VIOLATION_FIELD_CHARS: Final = 300
_ANALYSIS_PRECLIP: Final = 10_000
"""분석 글 하나를 정리하기 전에 미리 자르는 길이(50만 자를 문자 단위로 훑지 않으려고)."""
_CUT_NOTICE: Final = "\n\n(본문이 길어 여기서 잘렸습니다)"
_NO_SECTION: Final = "- (응답에 해당 절이 없습니다)"

_MENTION: Final = re.compile(r"@(?=[\w-])")
_CLOSING_KEYWORD: Final = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
    r"(?=[\s:]*(?:[\w.-]+/[\w.-]+#[0-9]|#[0-9]|https?://\S{0,200}?/(?:issues|pull)/[0-9]))",
    re.IGNORECASE,
)
# 위 정규식은 GitHub가 머지 때 이슈를 닫는 키워드를 찾는다. 키워드 뒤에서 처음 보는 부분은
# 공백·콜론 한 줄 수량자(`[\s:]*`)뿐이고 나머지는 길이가 정해졌거나 키워드마다 한 번만 훑으므로
# 입력 길이에 선형이다(중첩 수량자 없음). 적대적 5MB 입력 시험이 있다.
_BACKTICKS: Final = re.compile(r"`+")
_FENCE_LINE: Final = re.compile(r" {0,3}(?P<run>`{3,}|~{3,})(?P<info>.*)", re.DOTALL)
_HEADING_START: Final = re.compile(r"#{1,6}(?:[ \t]|$)")
_MASK_CHUNK: Final = 1500
_RUN_ID: Final = re.compile(r"[0-9]{8}-[0-9]{4}")
_LADDER: Final[Sequence[tuple[int | None, int]]] = (
    (None, 3000),
    (20, 1500),
    (10, 800),
    (5, 400),
    (0, 0),
)
"""(목록 항목 수 상한, 문서 분석 글자 상한). `None`은 `_MAX_LISTED`."""


# ---------------------------------------------------------------- 작은 도구


def extract_section(text: str, heading: str) -> str | None:
    """`heading` 줄 다음부터 다음 `#`·`##` 제목 전까지. 없거나 비면 `None`."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        return None
    rest: list[str] = []
    for line in lines[start + 1 :]:
        if re.match(r"#{1,2} ", line):
            break
        rest.append(line)
    body = "\n".join(rest).strip()
    return body or None


def neutralize_mentions(text: str) -> str:
    """GitHub가 알림·부수 효과를 일으킬 글을 무력화한다(멱등, 입력 길이에 선형).

    - `@` 뒤에 영숫자·`-`·`_`가 오면 `@` 다음에 U+200B를 넣는다(이메일도 바뀌지만 허용한다).
    - `Closes #5`·`fixes org/other#3`처럼 머지 때 이슈를 닫는 키워드는 그 다음에 U+200B를 넣는다.

    `#123` 같은 이슈 링크 자체는 사람을 울리지 않으므로 둔다.
    """
    return _CLOSING_KEYWORD.sub(lambda m: m.group() + ZWSP, _MENTION.sub("@" + ZWSP, text))


def fence(text: str) -> str:
    """내용 속 가장 긴 백틱 연속보다 하나 긴(최소 3개) 백틱 펜스로 감싼다."""
    longest = max((len(run) for run in _BACKTICKS.findall(text)), default=0)
    marker = "`" * max(3, longest + 1)
    return f"{marker}\n{text}\n{marker}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit)] + "…"


def _clean_body(text: str) -> str:
    """줄바꿈과 U+200B만 남기고 제어·서식·방향 문자를 이스케이프한다(`deliver`의 본문용)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(
        ZWSP.join(clean(part) for part in line.split(ZWSP)) for line in normalized.split("\n")
    )


def _mask(line: str) -> str:
    """한 줄에서 비밀값처럼 보이는 단어를 가리고 제어 문자를 이스케이프한다(`safe_text`).

    `safe_text`는 앞 2000자만 보므로 긴 줄은 공백에서 끊어 조각마다 부른다.
    """
    if len(line) <= _MASK_CHUNK:
        return safe_text(line, 1 << 30)
    parts: list[str] = []
    pos = 0
    while pos < len(line):
        end = min(len(line), pos + _MASK_CHUNK)
        if end < len(line):
            cut = max(line.rfind(" ", pos, end), line.rfind("\t", pos, end))
            if cut > pos:
                end = cut + 1
        parts.append(safe_text(line[pos:end], 1 << 30))
        pos = end
    return "".join(parts)


def _defuse_structure(line: str) -> str:
    """줄 앞에서 마크다운 블록(제목·밑줄 제목·펜스)이 시작되면 그 앞에 U+200B를 넣는다.

    들여쓰기·인용(`>`)·목록 표지(`-`·`+`·`*`·`1.`·`2)`)는 건너뛴 뒤의 나머지를 본다 — 목록 안 펜스도
    펜스다. 그래서 모델이 쓴 글은 펜스를 열거나 닫을 수 없고 우리 절 제목을 위조할 수 없다.
    """
    n = len(line)
    i = 0
    while i < n:
        c = line[i]
        if c in " \t>":
            i += 1
        elif c in "-+*" and i + 1 < n and line[i + 1] in " \t":
            i += 1
        elif "0" <= c <= "9":
            j = i
            while j < n and j - i < 9 and "0" <= line[j] <= "9":
                j += 1
            if j < n and line[j] in ".)" and j + 1 < n and line[j + 1] in " \t":
                i = j + 1
            else:
                break
        else:
            break
    rest = line[i:]
    ruled = rest.rstrip(" \t")
    if (
        rest.startswith(("```", "~~~"))
        or _HEADING_START.match(rest) is not None
        or (ruled != "" and (ruled.strip("=") == "" or ruled.strip("-") == ""))
    ):
        return line[:i] + ZWSP + rest
    return line


def _untrusted_line(text: str, *, structure: bool = True) -> str:
    """믿을 수 없는 한 줄(목록 항목, 제목 글): 한 줄로 만들고 비밀값을 가리고 HTML·구조를 막는다.

    - `&` -> `&amp;`, `<` -> `&lt;`: 원시 HTML과 `&#64;`·`&commat;` 같은 엔티티 멘션 우회를 막는다.
    - `![` -> `!`+U+200B+`[`: 이미지(추적 픽셀)를 막는다.
    """
    escaped = _mask(text).replace("&", "&amp;").replace("<", "&lt;").replace("![", "!" + ZWSP + "[")
    return _defuse_structure(escaped) if structure else escaped


def _untrusted_text(text: str) -> str:
    """믿을 수 없는 여러 줄 글(모델 응답): 줄마다 `_untrusted_line`. 줄바꿈은 남긴다."""
    normalized = text[:_ANALYSIS_PRECLIP].replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(_untrusted_line(line) for line in normalized.split("\n"))


def _item(text: str) -> str:
    """목록 항목 하나: 길이를 자르고 한 줄로 만든다."""
    return _untrusted_line(_clip(text, _ITEM_CHARS))


def _open_fence_closer(text: str) -> str | None:
    """글 끝까지 닫히지 않은 펜스를 닫는 줄(없으면 `None`). 줄 맨 앞(0~3칸)의 펜스만 본다.

    모델이 쓴 글에는 펜스가 남지 않으므로(`_defuse_structure`) 여기서 보이는 펜스는 우리 것이다.
    """
    marker: str | None = None
    for line in text.split("\n"):
        match = _FENCE_LINE.fullmatch(line)
        if match is None:
            continue
        run = match.group("run")
        info = match.group("info")
        if marker is None:
            if run[0] == "`" and "`" in info:
                continue  # 백틱 펜스의 안내 글에는 백틱이 올 수 없다
            marker = run
        elif run[0] == marker[0] and len(run) >= len(marker) and info.strip(" \t") == "":
            marker = None
    return marker


def _hard_cut(body: str, max_chars: int) -> str:
    """줄임 단계를 모두 써도 긴 본문을 `max_chars` 안으로 자른다. 열린 펜스는 닫는다."""
    budget = max_chars - len(_CUT_NOTICE)
    if budget < 1:
        return "…"[: max(0, max_chars)]
    prefix = body[:budget]
    for _ in range(12):
        closer = _open_fence_closer(prefix)
        if closer is None:
            return prefix + _CUT_NOTICE
        closing = "\n" + closer
        if len(prefix) + len(closing) <= budget:
            return prefix + closing + _CUT_NOTICE
        prefix = body[: max(0, budget - len(closing))]
    return "…"


# ---------------------------------------------------------------- 본문


@dataclass(frozen=True)
class Analysis:
    title: str
    text: str


@dataclass(frozen=True)
class PrBodyInput:
    reflected: Sequence[str]
    changed_pages: Sequence[str]
    new_questions: Sequence[str]
    resolved_questions: Sequence[str]
    analyses: Sequence[Analysis]
    low_confidence: str | None
    lint_violations: Sequence[LintViolation]
    warnings: Sequence[str]
    run_id: str
    cost_usd: float
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class _Items:
    shown: list[str]
    total: int


def _items(values: Sequence[str]) -> _Items:
    return _Items([_item(v) for v in values[:_MAX_LISTED]], len(values))


def _list_section(title: str, items: _Items, limit: int | None) -> str:
    if items.total == 0:
        return f"## {title}\n- 없음"
    shown = items.shown if limit is None else items.shown[:limit]
    lines = [f"- {i}" for i in shown]
    if items.total > len(shown):
        lines.append(f"- … 외 {items.total - len(shown)}건")
    return f"## {title}\n" + "\n".join(lines)


def _violation_block(violations: Sequence[LintViolation], total: int) -> str:
    """점검 위반 펜스 블록. `violations`는 이미 이스케이프하고 자른 것이다."""
    text = format_violations(violations)
    if total > len(violations):
        lines = text.split("\n")[:-1]  # 마지막 "위반 N건"은 전체 수로 다시 쓴다
        lines += [f"… 외 {total - len(violations)}건", f"위반 {total}건"]
        text = "\n".join(lines)
    return f"> ⚠️ 점검 실패 {total}건 — 머지 전에 확인하세요.\n\n{fence(text)}"


def _safe_violation(v: LintViolation) -> LintViolation:
    return replace(
        v,
        file=_mask(_clip(v.file, _VIOLATION_FIELD_CHARS)),
        message=_mask(_clip(v.message, _VIOLATION_FIELD_CHARS)),
    )


def _stats_line(inp: PrBodyInput) -> str:
    cost = (
        inp.cost_usd
        if isinstance(inp.cost_usd, (int, float)) and math.isfinite(inp.cost_usd)
        else 0.0
    )
    run_id = _untrusted_line(_clip(inp.run_id, _RUN_ID_CHARS), structure=False)
    return (
        f"runId {run_id} · 비용 ${max(cost, 0.0):.2f} · "
        f"입력 {int(inp.input_tokens):,} / 출력 {int(inp.output_tokens):,} 토큰"
    )


def build_pr_body(inp: PrBodyInput, max_chars: int) -> str:
    """절 순서대로 본문을 쓴다. 길면 `_LADDER` 순으로 목록과 분석 글을 줄이고, 그래도 길면 자른다.

    돌려주는 글의 길이는 멘션 무력화(U+200B)까지 더한 뒤 기준으로 `max_chars` 이하다.
    """
    if max_chars <= 0:
        return ""
    reflected = _items(inp.reflected)
    pages = _items(inp.changed_pages)
    questions = _Items(
        [
            *(_item(f"새로 생김: {t}") for t in inp.new_questions[:_MAX_LISTED]),
            *(_item(f"해소: {t}") for t in inp.resolved_questions[:_MAX_LISTED]),
        ][:_MAX_LISTED],
        len(inp.new_questions) + len(inp.resolved_questions),
    )
    warnings = _items(inp.warnings)
    violations = [_safe_violation(v) for v in inp.lint_violations[:_MAX_LISTED]]
    analyses = [
        (_untrusted_line(_clip(a.title, _ANALYSIS_TITLE_CHARS), structure=False), a.text)
        for a in inp.analyses[:_MAX_LISTED]
    ]
    analysis_cache: dict[int, str] = {}  # 정리는 글마다 한 번만(줄임 단계마다 다시 하지 않는다)

    def analysis_text(index: int) -> str:
        if index not in analysis_cache:
            analysis_cache[index] = _untrusted_text(analyses[index][1])
        return analysis_cache[index]

    low_confidence = (
        _NO_SECTION if inp.low_confidence is None else _untrusted_text(inp.low_confidence)
    )
    stats = _stats_line(inp)

    def render(limit: int | None, analysis_chars: int) -> str:
        parts: list[str] = []
        if inp.lint_violations:
            shown = violations if limit is None else violations[:limit]
            parts.append(_violation_block(shown, len(inp.lint_violations)))
        parts.append(_list_section("반영한 것", reflected, limit))
        parts.append(_list_section("바뀐 위키 페이지", pages, limit))
        parts.append(_list_section("미결", questions, limit))
        if inp.analyses:
            count = (
                len(analyses) if limit is None else min(len(analyses), max(limit, _ANALYSIS_FLOOR))
            )
            blocks = [
                f"### {analyses[i][0]}\n{_clip(analysis_text(i), analysis_chars)}"
                for i in range(count)
            ]
            if len(inp.analyses) > count:
                blocks.append(f"- … 외 {len(inp.analyses) - count}건")
            parts.append("## 문서별 분석\n" + "\n\n".join(blocks))
        parts.append(f"## 확신이 낮은 부분\n{_clip(low_confidence, _LOW_CONFIDENCE_CHARS)}")
        if inp.warnings:
            parts.append(_list_section("경고", warnings, limit))
        parts.append(f"## 실행 정보\n{stats}")
        return neutralize_mentions("\n\n".join(parts))

    body = ""
    for limit, analysis_chars in _LADDER:
        body = render(limit, analysis_chars)
        if len(body) <= max_chars:
            return body
    return _hard_cut(body, max_chars)


# ---------------------------------------------------------------- 전달


@dataclass(frozen=True)
class Delivered:
    result: Literal["pr", "direct"]
    pr_id: str | None


def _safe_title(title: str) -> str:
    """PR 제목: 한 줄, 비밀값 가림, 멘션·이슈 닫기 무력화, 무력화 뒤 300자 이하.

    GitHub는 제목을 글자 그대로 보여 주므로 `&`·`<`는 바꾸지 않는다(엔티티로 바꾸면 그대로 보인다).
    """
    one_line = _mask(title[: MAX_TITLE_CHARS * 2].strip()).strip()
    neutral = neutralize_mentions(one_line or DEFAULT_TITLE)
    return neutral if len(neutral) <= MAX_TITLE_CHARS else neutral[: MAX_TITLE_CHARS - 1] + "…"


def _check_deliver_args(
    *,
    kind: object,
    run_id: object,
    branch: object,
    base: object,
    title: object,
    body: object,
    lint_failed: object,
    review_mode: object,
    pr_port: PrPort | None,
) -> None:
    """git에 닿기 전에 값을 검사한다. 메시지에 값을 싣지 않는다."""
    if kind not in SYNC_LABELS:
        raise ValueError("PR 종류가 올바르지 않습니다")
    if review_mode not in ("pr", "direct"):
        raise ValueError("검토 방식이 올바르지 않습니다")
    if not (isinstance(branch, str) and is_safe_branch(branch)):
        raise ValueError("작업 브랜치 이름 형식이 올바르지 않습니다")
    if not (isinstance(base, str) and is_safe_branch(base)):
        raise ValueError("기준 브랜치 이름 형식이 올바르지 않습니다")
    if not (isinstance(title, str) and isinstance(body, str)):
        raise ValueError("PR 제목·본문은 문자열이어야 합니다")
    if not isinstance(lint_failed, bool):
        raise ValueError("lint_failed는 불리언이어야 합니다")
    if review_mode == "direct" and lint_failed:
        raise ValueError("점검 실패 상태로는 직접 반영할 수 없습니다")
    if review_mode == "pr":
        if pr_port is None:
            raise ValueError("PR 모드인데 PR 포트가 없습니다")
        if branch == base:
            raise ValueError("작업 브랜치가 기준 브랜치와 같습니다")
        if not (isinstance(run_id, str) and _RUN_ID.fullmatch(run_id)):
            raise ValueError("run_id 형식이 올바르지 않습니다")


async def deliver(
    *,
    git: GitClient,
    kind: SyncKind,
    run_id: str,
    branch: str,
    base: str,
    title: str,
    body: str,
    lint_failed: bool,
    review_mode: Literal["pr", "direct"],
    pr_port: PrPort | None,
    state_dir: Path,
) -> Delivered:
    """검토 방식에 따라 변경을 전달한다.

    - `direct`: `HEAD`를 `base`에 강제 없이 push한다. 빨리 감기가 아니면(다른 사람이 먼저 올렸으면)
      `GitError`다. 원격은 그대로이고 로컬 커밋도 남으므로 반영이 사라지지 않는다 — 호출자가 단계를
      실패로 처리한다.
    - `pr`: `HEAD`를 작업 브랜치에 push → PR 열기 → 추적 파일에 덧붙이기.

    제목은 한 줄·멘션 무력화·300자 이하로, 본문은 멘션 무력화·제어 문자 이스케이프를 한 번 더 건다
    (`build_pr_body`의 결과는 이미 안전하며 이 처리는 멱등이다).
    """
    _check_deliver_args(
        kind=kind,
        run_id=run_id,
        branch=branch,
        base=base,
        title=title,
        body=body,
        lint_failed=lint_failed,
        review_mode=review_mode,
        pr_port=pr_port,
    )
    head = await rev_parse(git, "HEAD")  # 기록한 SHA를 올린다("HEAD"를 두 번 풀지 않는다)
    if review_mode == "direct":
        await run_to_completion(push(git, head, base))
        return Delivered(result="direct", pr_id=None)
    assert pr_port is not None  # _check_deliver_args가 보장한다
    await run_to_completion(push(git, head, branch))
    tracked = await pr_port.open(
        OpenPrRequest(
            kind=kind,
            run_id=run_id,
            branch=branch,
            base=base,
            title=_safe_title(title),
            body=neutralize_mentions(_clean_body(body)),
            lint_failed=lint_failed,
            bot_head_sha=head,
        )
    )
    others = [pr for pr in read_tracking(state_dir) if pr.id != tracked.id]
    write_tracking(state_dir, [*others, tracked])
    return Delivered(result="pr", pr_id=tracked.id)
