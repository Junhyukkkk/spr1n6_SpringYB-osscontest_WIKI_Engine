import dataclasses
import math
from typing import get_args

import pytest

from team_wiki_engine.claude.types import RunClaudeResult, Usage
from team_wiki_engine.sync.types import StageResult, StageResultKind, SyncKind, add_usage, new_stage


def _result(
    *, cost_usd: float = 0.25, input_tokens: int = 10, output_tokens: int = 2, duration_ms: int = 7
) -> RunClaudeResult:
    return RunClaudeResult(
        ok=True,
        text="",
        session_id=None,
        cost_usd=cost_usd,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        duration_ms=duration_ms,
        tool_calls=[],
    )


def test_literal_values() -> None:
    assert get_args(SyncKind) == ("code", "ingest")
    assert get_args(StageResultKind) == ("skipped-open-pr", "no-change", "pr", "direct", "failed")


class TestNewStage:
    def test_defaults(self) -> None:
        stage = new_stage("code")
        assert stage == StageResult(kind="code")
        assert stage.kind == "code"
        assert stage.result == "no-change"
        assert stage.pr_id is None
        assert stage.lint_failed is False
        assert (stage.cost_usd, stage.input_tokens, stage.output_tokens) == (0.0, 0, 0)
        assert stage.duration_ms == 0
        assert stage.summary == ""
        assert stage.warnings == []
        assert stage.error is None

    def test_ingest_kind(self) -> None:
        assert new_stage("ingest").kind == "ingest"

    def test_warnings_list_is_not_shared(self) -> None:
        a = new_stage("code")
        b = new_stage("ingest")
        a.warnings.append("경고")
        assert b.warnings == []

    def test_is_mutable_dataclass(self) -> None:
        stage = new_stage("code")
        stage.result = "pr"
        stage.pr_id = "7"
        assert dataclasses.is_dataclass(stage)
        assert (stage.result, stage.pr_id) == ("pr", "7")


class TestAddUsage:
    def test_sums_cost_tokens_and_duration(self) -> None:
        stage = new_stage("code")
        add_usage(stage, _result())
        add_usage(stage, _result())
        assert stage.cost_usd == 0.5
        assert (stage.input_tokens, stage.output_tokens, stage.duration_ms) == (20, 4, 14)

    def test_bool_cost_counts_as_zero(self) -> None:
        stage = new_stage("code")
        add_usage(stage, _result(cost_usd=True))
        assert stage.cost_usd == 0.0

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1.5])
    def test_non_finite_or_negative_cost_counts_as_zero(self, bad: float) -> None:
        stage = new_stage("code")
        add_usage(stage, _result(cost_usd=0.5))
        add_usage(stage, _result(cost_usd=bad))
        assert stage.cost_usd == 0.5
        assert math.isfinite(stage.cost_usd)
        # 비용만 0으로 본다. 토큰·시간은 그대로 더한다.
        assert (stage.input_tokens, stage.output_tokens, stage.duration_ms) == (20, 4, 14)
