"""들어오는 질문과 나가는 답을 다루는 순수 함수: 멘션 판정, 정리, 가리기, 해시."""

import hashlib
import os
import re
import unicodedata
from collections.abc import Sequence
from typing import Final

from team_wiki_engine.bot.split import close_open_fence, utf16_len
from team_wiki_engine.config.load import Paths
from team_wiki_engine.ops.notify import redact_secrets, truncate_utf16
from team_wiki_engine.sync.stage import path_roots
from team_wiki_engine.wiki.lint_secrets import clean, mask_secret_tokens, shorten

SNOWFLAKE: Final = re.compile(r"[0-9]{1,25}")
MAX_QUESTION_UNITS: Final = 4000
MAX_RECORD_CHARS: Final = 1000
THREAD_NAME_UNITS: Final = 50

_ZWSP = "​"
_EVERYONE = re.compile(r"@(everyone|here)")
_EVIDENCE_NOT_FOUND = re.compile(r"[—–-]\s*근거\s*:\s*찾지\s*못함\s*[.。]?")
_MAX_EVIDENCE_LINE = 200
_WHITESPACE_RUN = re.compile(r"\s+")


def is_snowflake(value: object) -> bool:
    return isinstance(value, str) and SNOWFLAKE.fullmatch(value) is not None


def _mention_pattern(bot_user_id: str) -> re.Pattern[str]:
    return re.compile(rf"<@!?{re.escape(bot_user_id)}>")


def mentions_bot(content: str, bot_user_id: str) -> bool:
    if not is_snowflake(bot_user_id):
        return False
    return _mention_pattern(bot_user_id).search(content) is not None


def extract_question(content: str, bot_user_id: str) -> str:
    if not is_snowflake(bot_user_id):
        return content.strip()
    return _mention_pattern(bot_user_id).sub("", content).strip()


def question_too_long(question: str) -> bool:
    return utf16_len(question) > MAX_QUESTION_UNITS


def clean_multiline(text: str) -> str:
    """줄바꿈은 지키고 제어·서식·방향 지정 문자는 `\\uXXXX`로 보이게 한다. 탭은 공백 4칸."""
    for newline in ("\r\n", "\r", " ", " "):
        text = text.replace(newline, "\n")
    text = text.replace("\t", "    ")
    return "\n".join(clean(line) for line in text.split("\n"))


def normalize_question(text: str) -> str:
    return clean_multiline(unicodedata.normalize("NFC", text)).strip()


def thread_name(question: str) -> str:
    collapsed = _WHITESPACE_RUN.sub(" ", question).strip()
    name = truncate_utf16(clean(unicodedata.normalize("NFC", collapsed)), THREAD_NAME_UNITS)
    return name or "질문"


def hash_user(user_id: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{user_id}".encode()).hexdigest()[:12]


def neutralize_everyone(text: str) -> str:
    return _EVERYONE.sub(lambda m: f"@{_ZWSP}{m.group(1)}", text)


def evidence_not_found(text: str) -> bool:
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or len(stripped) > _MAX_EVIDENCE_LINE:
            continue
        normalized = unicodedata.normalize("NFKC", stripped).strip()
        if _EVIDENCE_NOT_FOUND.fullmatch(normalized):
            return True
    return False


def answer_roots(paths: Paths) -> tuple[tuple[str, str], ...]:
    """답에서 바꿀 (서버 경로, 바꿀 글) 목록. `data/`·`repos/` 접두는 지우고 나머지는 표지로."""
    candidates: list[tuple[str, str]] = [
        (f"{paths.data_dir}/", ""),
        (f"{paths.repos_dir}/", ""),
        *((root, token) for token, root in path_roots(paths).items()),
    ]
    out: list[tuple[str, str]] = []
    for root, replacement in candidates:
        if len(root.rstrip("/")) > 1 and (root, replacement) not in out:
            out.append((root, replacement))
        try:
            real = os.path.realpath(root)
        except (OSError, ValueError):
            continue
        if root.endswith("/"):
            real += "/"
        if real != root and len(real.rstrip("/")) > 1 and (real, replacement) not in out:
            out.append((real, replacement))
    return tuple(out)


def scrub_answer_paths(text: str, roots: Sequence[tuple[str, str]]) -> str:
    """서버 루트 경로만 바꾼다. `/api/v1/users` 같은 일반 절대 경로는 그대로 둔다."""
    for root, replacement in sorted(roots, key=lambda item: -len(item[0])):
        text = text.replace(root, replacement)
    return text


def mask_known(text: str, redact: Sequence[str]) -> str:
    return mask_secret_tokens(redact_secrets(text, redact))


def safe_answer(text: str, *, redact: Sequence[str], roots: Sequence[tuple[str, str]]) -> str:
    masked = mask_known(text, redact)
    scrubbed = scrub_answer_paths(masked, roots)
    return close_open_fence(neutralize_everyone(clean_multiline(scrubbed))).strip()


def record_text(text: str, redact: Sequence[str]) -> str:
    masked = mask_known(text, redact)
    if len(masked) <= MAX_RECORD_CHARS:
        return masked
    return shorten(masked, MAX_RECORD_CHARS - 1)
