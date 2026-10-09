"""Claude 실행기 가짜. 실제 Claude는 절대 부르지 않는다."""

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from team_wiki_engine.claude.types import RunClaudeInput, RunClaudeResult, Usage

Script = Callable[[RunClaudeInput, int], Mapping[str, Any] | None]
"""`(입력, 호출 번호)`로 불린다. `inp.cwd`에 파일을 쓰고, 바꿀 결과 필드만 돌려준다."""


def ok_result(**overrides: Any) -> RunClaudeResult:
    """성공 결과. `overrides`로 필드를 덮어쓴다."""
    base = RunClaudeResult(
        ok=True,
        text="완료",
        session_id="sess-1",
        cost_usd=0.01,
        usage=Usage(100, 10, 0, 0),
        duration_ms=5,
        tool_calls=[],
    )
    return dataclasses.replace(base, **overrides)


class ScriptedRunner:
    """호출마다 스크립트를 실행하는 `ClaudeRunner`. 스크립트가 없으면 `ok_result()`."""

    def __init__(self, script: Script | None = None) -> None:
        self.inputs: list[RunClaudeInput] = []
        self._script = script

    def set_script(self, script: Script) -> None:
        self._script = script

    async def __call__(self, inp: RunClaudeInput) -> RunClaudeResult:
        self.inputs.append(inp)
        if self._script is None:
            return ok_result()
        overrides = self._script(inp, len(self.inputs) - 1)
        return ok_result(**(overrides or {}))


class Drain:
    """`drain(timeout)`을 부른 횟수·인자를 기록하고 정해 둔 개수를 돌려주는 가짜."""

    def __init__(self, pending: int = 0) -> None:
        self.pending = pending
        self.calls: list[float] = []

    async def __call__(self, timeout: float) -> int:
        self.calls.append(timeout)
        return self.pending
