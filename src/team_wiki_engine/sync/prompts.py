"""동기화 프롬프트 공통(G2): 첫 줄 오늘 날짜, NFC 파일 이름, 제어 문자 이스케이프.

프롬프트에 들어가는 글은 신뢰할 수 없는 것(파일 이름, 점검 메시지)이 많다. 줄바꿈·서식 문자가
그대로 들어가면 프롬프트 한 줄을 다른 줄로 위조할 수 있으므로 한 줄에 들어가는 글은 모두
`prompt_name`/`clean`을 거친다. 코드 반영·문서 반영·재확인 프롬프트도 이 모듈에 있다.
"""

import dataclasses
import re
import unicodedata
from collections.abc import Sequence

from team_wiki_engine.sync.delta import DeltaSummary, delta_json
from team_wiki_engine.wiki.lint import LintViolation, format_violations
from team_wiki_engine.wiki.lint_secrets import clean, shorten

MAX_PROMPT_VIOLATIONS = 200
"""수정 지시문에 싣는 위반의 최대 개수. 넘으면 나머지는 `외 N건`으로 센다(프롬프트 크기 상한)."""
MAX_NAME_CHARS = 200
"""`prompt_name` 결과의 최대 길이(말줄임표 포함)."""
MAX_RECHECK_TARGETS = 200
"""재확인 지시문에 싣는 경로의 최대 개수. 넘으면 나머지는 `- 외 N건`으로 센다."""
MAX_VIOLATION_FIELD_CHARS = 500
"""위반 한 건의 파일·메시지 각각의 최대 길이(넘으면 `…`)."""

_PARTIAL_ESCAPE = re.compile(r"\\(?:x[0-9a-f]?|u[0-9a-f]{0,3}|U[0-9a-f]{0,7})?$")
_TODAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def today_line(today: str) -> str:
    """모든 동기화 프롬프트의 첫 줄. `today`는 KST `YYYY-MM-DD`(ASCII 숫자)여야 한다.

    형식이 아니면 `ValueError`다(입력 내용은 메시지에 싣지 않는다 — 줄 위조 방지).
    """
    if _TODAY.fullmatch(today) is None:
        raise ValueError("오늘 날짜는 YYYY-MM-DD 형식이어야 합니다")
    return "오늘 날짜: " + today


def prompt_name(name: str) -> str:
    """프롬프트에 넣을 파일 이름: NFC로 바꾸고 제어·서식·방향 지정 문자를 이스케이프한다.

    이스케이프로 늘어난 뒤의 길이가 200자를 넘으면 `…`로 끝나게 자른다(합쳐서 200자).
    """
    return cut_escaped(clean(unicodedata.normalize("NFC", name)), MAX_NAME_CHARS)


def cut_escaped(text: str, limit: int) -> str:
    """`clean`을 거친 글을 `limit`자(말줄임표 포함) 이하로 자른다. 끊긴 이스케이프 조각은 뺀다."""
    if len(text) <= limit:
        return text
    cut = _PARTIAL_ESCAPE.sub("", text[: limit - 1])  # 끊긴 `\x1`·`\` 조각은 뺀다
    return f"{cut}…"


def _line_safe(text: str) -> str:
    return shorten(clean(text), MAX_VIOLATION_FIELD_CHARS)


def lint_fix_prompt(violations: Sequence[LintViolation], today: str) -> str:
    """점검 위반을 같은 세션에서 한 번 고치게 하는 지시문."""
    shown = [
        dataclasses.replace(v, file=_line_safe(v.file), message=_line_safe(v.message))
        for v in violations[:MAX_PROMPT_VIOLATIONS]
    ]
    listing = format_violations(shown)
    omitted = len(violations) - len(shown)
    if omitted > 0:  # `format_violations`의 꼬리("위반 N건")는 일부만 센 값이라 빼고 전체를 적는다
        body = listing.rsplit("\n", 1)[0]
        listing = (
            f"{body}\n외 {omitted}건 (전체 {len(violations)}건)은 생략했습니다. "
            "위 위반부터 고치세요."
        )
    return "\n".join(
        [
            today_line(today),
            "점검에서 아래 위반이 나왔습니다. wiki/ 아래 파일만 고쳐서 해결하세요.",
            "raw/, baseline.json, wiki/log.md는 건드리지 마세요.",
            "",
            listing,
        ]
    )


def code_sync_prompt(summary: DeltaSummary, today: str) -> str:
    """코드 반영 지시문. `delta_json`이 백틱을 이스케이프하므로 JSON 펜스는 깨지지 않는다."""
    return "\n".join(
        [
            today_line(today),
            "아래 DeltaSummary를 읽고 sync-code 규칙대로 wiki/ 아래 페이지를 고치세요.",
            "baseline.json과 wiki/log.md는 스크립트가 씁니다. 건드리지 마세요.",
            "",
            "```json",
            delta_json(summary),
            "```",
        ]
    )


def ingest_prompt(file: str, processed: Sequence[str], today: str) -> str:
    """원본 하나를 반영하라는 지시문. 이름은 `prompt_name`을 거쳐 항상 한 줄에 들어간다."""
    done = ", ".join(map(prompt_name, processed)) or "없음"
    return "\n".join(
        [
            today_line(today),
            f"원본 파일: raw/{prompt_name(file)}",
            f"이번 실행에서 앞서 반영한 원본: {done}",
            "ingest 규칙대로 이 원본 하나만 위키에 반영하세요. raw/는 고치지 마세요.",
            "baseline.json과 wiki/log.md는 스크립트가 씁니다. 건드리지 마세요.",
        ]
    )


def recheck_prompt(changed: Sequence[str], today: str) -> str:
    """바뀐 결정·약속에 기댄 페이지의 낡은 서술을 한 번 고치게 하는 지시문."""
    shown = changed[:MAX_RECHECK_TARGETS]
    omitted = len(changed) - len(shown)
    return "\n".join(
        [
            today_line(today),
            "재확인 요청입니다. 새 원본은 반영하지 마세요.",
            "이번 실행에서 바뀐 결정·약속:",
            *(f"- {prompt_name(c)}" for c in shown),
            *([f"- 외 {omitted}건"] if omitted > 0 else []),
            "이것들에 기대는 페이지를 찾아 낡은 서술을 고치세요. "
            "고칠 것이 없으면 아무것도 바꾸지 마세요.",
        ]
    )
