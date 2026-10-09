"""계획 1의 모듈 사이 이음새: 실제 설정·공개 쓰기 함수·러너 옵션을 이어서 확인한다."""

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.claude.profiles import PROFILES
from team_wiki_engine.claude.runner import RunnerDeps, build_options
from team_wiki_engine.claude.types import RunClaudeInput
from team_wiki_engine.config.load import load_config, resolve_paths
from team_wiki_engine.config.schema import EffortConfig
from team_wiki_engine.util.fs import write_file_atomic
from team_wiki_engine.wiki.baseline import Baseline, RepoBaseline, write_baseline
from team_wiki_engine.wiki.frontmatter import serialize_frontmatter
from team_wiki_engine.wiki.lint import lint
from team_wiki_engine.wiki.log import LogEntry, append_log

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "config" / "team.example.json"


# ---------------------------------------------------------------- 설정 → 러너


def _inputs_for_all_profiles(data: Path, repos: Path) -> list[RunClaudeInput]:
    return [
        RunClaudeInput(
            profile=name,
            prompt="질문",
            cwd=data,
            read_roots=(repos,),
            write_roots=() if name == "bot" else (data / "wiki",),
            timeout_ms=1000,
        )
        for name in PROFILES
    ]


@pytest.mark.parametrize("as_dict", [False, True], ids=["EffortConfig", "dict"])
def test_example_config_builds_runner_options_for_every_profile(as_dict: bool) -> None:
    config = load_config(EXAMPLE)
    paths = resolve_paths({"TEAM_WIKI_HOME": "/srv/seam-test"}, REPO_ROOT)
    effort: Any = (
        {"bot": config.effort.bot, "sync": config.effort.sync} if as_dict else config.effort
    )
    assert as_dict or isinstance(effort, EffortConfig)
    deps = RunnerDeps(
        model=config.model, effort=effort, skills_dir=paths.skills_dir, env={"PATH": "/usr/bin"}
    )
    seen = {}
    for inp in _inputs_for_all_profiles(paths.data_dir, paths.repos_dir):
        options = build_options(inp, deps)
        expected = (
            config.effort.bot if PROFILES[inp.profile].effort_key == "bot" else (config.effort.sync)
        )
        assert options.effort == expected
        assert options.model == config.model
        assert options.cwd == str(paths.data_dir)
        seen[inp.profile] = options.effort
    assert set(seen) == {"bot", "sync-code", "ingest"}


def test_runner_deps_rejects_effort_without_both_keys_at_construction(tmp_dir: Path) -> None:
    with pytest.raises(ValueError, match="sync"):
        RunnerDeps(model="m", effort={"bot": "low"}, skills_dir=tmp_dir, env={})
    with pytest.raises(ValueError, match="bot"):
        RunnerDeps(model="m", effort={}, skills_dir=tmp_dir, env={})


def test_runner_deps_rejects_unknown_effort_values_and_types(tmp_dir: Path) -> None:
    with pytest.raises(ValueError, match="effort"):
        RunnerDeps(model="m", effort={"bot": "turbo", "sync": "low"}, skills_dir=tmp_dir, env={})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="effort"):
        RunnerDeps(model="m", effort="low", skills_dir=tmp_dir, env={})  # type: ignore[arg-type]


def test_runner_deps_still_works_with_dataclasses_replace(tmp_dir: Path) -> None:
    deps = RunnerDeps(model="m", effort=EffortConfig(), skills_dir=tmp_dir, env={})
    assert dataclasses.replace(deps, model="n").effort == EffortConfig()


# ---------------------------------------------------------------- 공개 쓰기 함수 → 점검


def _page(data_dir: Path, rel: str, front: dict[str, Any], body: str) -> None:
    write_file_atomic(data_dir / rel, serialize_frontmatter(front, body))


def test_data_dir_built_only_through_public_writers_passes_lint(tmp_dir: Path) -> None:
    data = tmp_dir / "data"
    sha = "b" * 40
    meeting = "2026-10-07-회의록.md"
    decision = "2026-10-07-relay-제거.md"
    write_file_atomic(data / "raw" / meeting, "# 회의록\n")
    day = {"updated": "2026-10-07"}
    _page(data, "wiki/index.md", day, "\n# 목차\n\n- [worker](status/worker.md)\n")
    _page(data, "wiki/overview.md", day, "\n# 개요\n")
    _page(data, "wiki/open-questions.md", day, "\n# 미결\n")
    _page(
        data,
        "wiki/status/worker.md",
        {
            "repo": "worker",
            "baseline_commit": sha,
            "archived": False,
            "owners": ["member-a"],
            **day,
        },
        "\n# worker\n",
    )
    _page(
        data,
        f"wiki/decisions/{decision}",
        {
            "status": "논의 중",
            "decided_at": "2026-10-07",
            "superseded_by": None,
            "sources": [meeting],
            **day,
        },
        "\n# relay 제거\n",
    )
    _page(
        data,
        f"wiki/sources/{meeting}",
        {"raw": meeting, "date": "2026-10-07", "affects": [f"decisions/{decision}"], **day},
        "\n# 요약\n",
    )
    write_baseline(
        data,
        Baseline(
            repos={"worker": RepoBaseline(commit=sha, archived=False)},
            raw_processed=[meeting],
            updated_at="2026-10-07T09:00:00+09:00",
        ),
    )
    append_log(
        data,
        LogEntry(
            at=datetime(2026, 10, 7, 1, 0, tzinfo=UTC),
            kind="ingest",
            targets=meeting,
            pages=[f"decisions/{decision}"],
            cost_usd=0.12,
        ),
    )
    assert json.loads((data / "baseline.json").read_text(encoding="utf-8"))["repos"]
    assert lint(data) == []
