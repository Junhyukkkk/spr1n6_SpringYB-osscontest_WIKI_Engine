"""Claude 실행 계층이 주고받는 타입."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ClaudeProfileName = Literal["bot", "sync-code", "ingest"]


@dataclass(frozen=True, slots=True)
class RunClaudeInput:
    profile: ClaudeProfileName
    prompt: str
    cwd: Path
    """데이터 사본 루트."""
    read_roots: tuple[Path, ...]
    """cwd 외에 읽기를 허용할 루트(코드 사본, 실행별 diff 폴더)."""
    write_roots: tuple[Path, ...]
    """쓰기를 허용할 루트(봇은 빈 튜플)."""
    timeout_ms: int
    resume_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallTrace:
    """도구 호출 한 건의 기록.

    `allowed`는 가드(path_guard)가 내린 판정일 뿐이다. 호출 시도가 있었다는 증거이지,
    SDK가 실제로 무엇을 실행했는지에 대한 증명이 아니다.
    `path`는 정규화한 절대 경로이며, 도구가 경로를 받지 않거나 가드가 경로 문법을
    거부했으면 `None`이다.
    """

    name: str
    path: Path | None
    allowed: bool


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(slots=True)
class RunClaudeResult:
    ok: bool
    text: str
    session_id: str | None
    cost_usd: float
    usage: Usage
    duration_ms: int
    tool_calls: list[ToolCallTrace]
    failure: Literal["timeout", "error", "empty", "max_turns"] | None = None
    detail: str | None = None


ClaudeRunner = Callable[[RunClaudeInput], Awaitable[RunClaudeResult]]
