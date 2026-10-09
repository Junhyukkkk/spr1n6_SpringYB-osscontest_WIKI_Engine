"""디스코드 2000 UTF-16 유닛 나누기. 코드 펜스(``` ~~~, 4개 이상)를 조각마다 닫고 다시 연다."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from team_wiki_engine.bot.replies import TRUNCATED_NOTE
from team_wiki_engine.ops.notify import DISCORD_LIMIT as _NOTIFY_LIMIT

DISCORD_LIMIT: Final = _NOTIFY_LIMIT
MAX_CHUNKS: Final = 8
MAX_REOPEN_UNITS: Final = 100

_ASTRAL = re.compile("[\U00010000-\U0010ffff]")
_FENCE_LINE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


def utf16_len(text: str) -> int:
    return len(text) + len(_ASTRAL.findall(text))


@dataclass(frozen=True)
class _Fence:
    char: str
    length: int
    line: str  # 여는 줄 그대로


def _step(state: _Fence | None, line: str, max_len: int) -> _Fence | None:
    match = _FENCE_LINE.fullmatch(line)
    if match is None:
        return state
    run, rest = match.group(1), match.group(2)
    if state is None:
        if len(run) > max_len or (run[0] == "`" and "`" in rest):
            return None
        return _Fence(run[0], len(run), line)
    if run[0] == state.char and len(run) >= state.length and rest.strip(" \t") == "":
        return None
    return state


def close_open_fence(text: str) -> str:
    """끝까지 읽어 코드 펜스가 열려 있으면 닫는다."""
    state: _Fence | None = None
    for line in text.split("\n"):
        state = _step(state, line, len(text) + 3)
    if state is None:
        return text
    return text + "\n" + state.char * state.length


def _cut_units(text: str, units: int) -> list[str]:
    """`units` 유닛 이하로 자른다(코드 포인트 단위라 서로게이트 쌍을 끊지 않는다)."""
    parts: list[str] = []
    start = 0
    used = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > units:
            parts.append(text[start:index])
            start = index
            used = 0
        used += width
    parts.append(text[start:])
    return parts


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    if text.strip() == "":
        return []
    max_fence = max(3, min(MAX_REOPEN_UNITS, limit // 8))
    max_reopen = max(3, min(MAX_REOPEN_UNITS, limit // 4))

    chunks: list[str] = []
    cur: list[str] = []  # 현재 조각의 줄(맨 앞은 다시 연 줄일 수 있다)
    cur_len = 0
    has_content = False
    state: _Fence | None = None

    def reopen_line(fence: _Fence) -> str:
        if utf16_len(fence.line) <= max_reopen:
            return fence.line
        return fence.char * fence.length

    def flush() -> None:
        nonlocal cur, cur_len, has_content
        if has_content:
            body = "\n".join(cur)
            if state is not None:
                body += "\n" + state.char * state.length
            if body.strip() != "":
                chunks.append(body)
        cur = []
        cur_len = 0
        has_content = False
        if state is not None:
            first = reopen_line(state)
            cur.append(first)
            cur_len = utf16_len(first)

    def add(line: str, after: _Fence | None) -> None:
        nonlocal cur_len, has_content, state
        width = utf16_len(line)
        reserve = 1 + after.length if after is not None else 0
        if cur and cur_len + 1 + width + reserve > limit:
            flush()
        cur.append(line)
        cur_len += (1 if len(cur) > 1 else 0) + width
        has_content = True
        state = after

    for line in text.split("\n"):
        after = _step(state, line, max_fence)
        fence = after or state
        reopen = utf16_len(reopen_line(fence)) if fence is not None else 0
        closing = fence.length + 1 if fence is not None else 0
        max_line = limit if fence is None else max(1, limit - 2 * closing - reopen - 16)
        if utf16_len(line) <= max_line:
            add(line, after)
            continue
        parts = _cut_units(line, max_line)
        opens = state is None and after is not None
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            if cur and has_content:
                flush()
            add(part, after if (last or opens) else state)
        # 마지막 조각 뒤에 이어지는 줄이 같은 조각에 붙을 수 있다.
    flush()
    return chunks


def cap_chunks(
    chunks: Sequence[str], max_chunks: int = MAX_CHUNKS, note: str = TRUNCATED_NOTE
) -> list[str]:
    if len(chunks) <= max_chunks:
        return list(chunks)
    return [*chunks[: max_chunks - 1], note]
