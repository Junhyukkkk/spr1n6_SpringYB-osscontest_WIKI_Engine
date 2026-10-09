"""답이 위키에서 왔는지 코드 사본을 읽어서 왔는지 판정한다.

경로 비교는 링크를 해소한 경로로 한다(문자열 접두어 비교 금지: `repos-evil`이 `repos`로
오인되거나 루트 안 링크가 밖을 가리키는 경우를 막는다). 예외를 던지지 않는다.
"""

import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from team_wiki_engine.claude.path_guard import READ_TOOLS, canonicalize, is_inside
from team_wiki_engine.claude.types import ToolCallTrace


def code_reads(calls: Sequence[ToolCallTrace], repos_dir: Path) -> list[Path]:
    """코드 사본 안을 읽은 호출의 경로(기록에 있는 그대로, 호출 순서대로).

    읽기 도구이고 가드가 허용했고 경로가 절대 경로이며 링크를 해소한 경로가 `repos_dir` 안인
    호출만 센다. 링크 순환처럼 해소할 수 없는 경로는 코드 읽기가 아니다.
    """
    reads: list[Path] = []
    for call in calls:
        path = call.path
        if call.name not in READ_TOOLS or not call.allowed or path is None:
            continue
        if not path.is_absolute() or not is_inside(path, [repos_dir]):
            continue
        reads.append(path)
    return reads


def answer_source(calls: Sequence[ToolCallTrace], repos_dir: Path) -> Literal["wiki", "code"]:
    return "code" if code_reads(calls, repos_dir) else "wiki"


def code_paths(calls: Sequence[ToolCallTrace], repos_dir: Path) -> list[str]:
    """읽은 코드의 `repos_dir` 기준 상대 경로(`/` 구분, NFC, 처음 나온 순서, 중복 없음).

    `repos_dir` 자체(저장소 전체를 훑은 Grep·Glob)는 상대 경로가 없으므로 목록에 넣지 않는다.
    """
    reads = code_reads(calls, repos_dir)
    if not reads:
        return []
    try:
        root = canonicalize(repos_dir)
    except (OSError, ValueError):
        return []
    seen: dict[str, None] = {}
    for path in reads:
        try:
            relative = canonicalize(path).relative_to(root)
        except (OSError, ValueError):
            continue
        text = unicodedata.normalize("NFC", relative.as_posix())
        if text != ".":
            seen.setdefault(text)
    return list(seen)
