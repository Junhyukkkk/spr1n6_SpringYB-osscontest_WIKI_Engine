"""동기화 단계 결과 타입과 비용 합산."""

import math
from dataclasses import dataclass, field
from typing import Literal

from team_wiki_engine.claude.types import RunClaudeResult

SyncKind = Literal["code", "ingest"]
StageResultKind = Literal["skipped-open-pr", "no-change", "pr", "direct", "failed"]


@dataclass
class StageResult:
    """단계 하나의 결과. 단계가 진행되면서 채워 가므로 가변이다."""

    kind: SyncKind
    result: StageResultKind = "no-change"
    pr_id: str | None = None
    lint_failed: bool = False
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def new_stage(kind: SyncKind) -> StageResult:
    return StageResult(kind=kind)


def add_usage(stage: StageResult, r: RunClaudeResult) -> None:
    """Claude 실행 한 번의 비용·토큰·시간을 더한다. 유한하지 않거나 음수인 비용은 0으로 본다."""
    cost = r.cost_usd
    # bool은 int의 하위 타입이라 따로 거른다(True가 1.0원이 되지 않게).
    if (
        isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and cost > 0
    ):
        stage.cost_usd += float(cost)
    stage.input_tokens += r.usage.input_tokens
    stage.output_tokens += r.usage.output_tokens
    stage.duration_ms += r.duration_ms
