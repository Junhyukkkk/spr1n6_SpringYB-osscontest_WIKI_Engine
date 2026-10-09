"""동기화 끝까지 가는 테스트(Task 18): 가짜 Claude, 실제 git, `delivery: local`, 네트워크 0.

`amain`을 환경변수·설정 파일부터 실제 조립(`build_sync_deps`)으로 돌리고, 러너와 시계만 바꾼다.
"""

import dataclasses
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from team_wiki_engine.config.load import Paths, Secrets
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.metrics.record import (
    OqEventRecord,
    PrReviewRecord,
    SyncRunRecord,
    read_jsonl,
)
from team_wiki_engine.sync.main import amain
from team_wiki_engine.sync.pull import apull
from team_wiki_engine.sync.stage import SyncDeps
from team_wiki_engine.sync.wiring import build_sync_deps
from tests.helpers.git import seed_bare
from tests.helpers.offline import OfflineGuard
from tests.helpers.runner import ScriptedRunner
from tests.helpers.sync_env import ENGINE_DIR, default_script

START = datetime(2026, 10, 7, 0, 0, 0, tzinfo=UTC)  # 서울 시각 09:00(실행 번호는 서울 시각)
EXPECTED_LINE = (
    "[ops] [sync 20261007-0900] 코드: 변경 1건 → 20261007-0900-code.md · "
    "문서: 1건 → 20261007-0900-ingest.md · 비용 $0.02"
)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


async def test_끝까지_간다(
    tmp_dir: Path, capsys: pytest.CaptureFixture[str], offline_guard: OfflineGuard
) -> None:
    root = tmp_dir
    code_bare = root / "worker.git"
    await seed_bare(code_bare, root / "worker-work", {"src/A.kt": "class A"})
    data_bare = root / "data.git"
    data_work = await seed_bare(
        data_bare,
        root / "data-work",
        {"raw/.gitkeep": "", "raw/2026-10-07-회의록.md": "# 회의록\n내용\n"},
    )
    git_home = root / "git-home"
    git_home.mkdir()
    home = root / "home"
    config_path = root / "team.json"
    config_path.write_text(
        json.dumps(
            {
                "repos": [{"name": "worker", "url": str(code_bare)}],
                "dataRepo": {"url": str(data_bare)},
            }
        ),
        encoding="utf-8",
    )
    env = {
        "TEAM_WIKI_HOME": str(home),
        "TEAM_WIKI_CONFIG": str(config_path),
        "TEAM_WIKI_ENGINE_DIR": str(ENGINE_DIR),
        "ANTHROPIC_API_KEY": "test-key-not-real",
        "PATH": os.environ["PATH"],
        "HOME": str(git_home),
    }

    clock = Clock(START)
    runner = ScriptedRunner(default_script)
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(500)

    def http_factory() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler), trust_env=False, follow_redirects=False
        )

    def build(
        config: TeamConfig, secrets: Secrets, paths: Paths, *, env: Any, http: httpx2.AsyncClient
    ) -> SyncDeps:
        deps = build_sync_deps(config, secrets, paths, env=env, http=http, now=clock)
        return dataclasses.replace(deps, runner=runner, now=clock)

    async def run() -> int:
        return await amain([], env, build_deps=build, http_factory=http_factory)

    state = home / "state"

    # 1) 첫 실행: 코드·문서 두 단계가 각각 PR 파일을 만든다.
    assert await run() == 0
    out = capsys.readouterr().out
    assert EXPECTED_LINE in out
    assert len(runner.inputs) == 2
    pr_files = sorted((state / "local-prs").iterdir())
    assert len(pr_files) == 2
    for file in pr_files:
        assert "점검 실패" not in file.read_text(encoding="utf-8")
    branches = (await data_work.run(["ls-remote", "--heads", str(data_bare)])).text
    assert "refs/heads/sync/code-" in branches
    assert "refs/heads/sync/ingest-" in branches
    runs = read_jsonl(state / "sync-runs.jsonl", SyncRunRecord)
    assert [r.result for r in runs] == ["pr", "pr"]
    assert requests == []

    # 2) 12시간 뒤: 열린 PR이 있어 두 단계 모두 건너뛴다.
    clock.value = START + timedelta(hours=12)
    assert await run() == 0
    capsys.readouterr()
    runs = read_jsonl(state / "sync-runs.jsonl", SyncRunRecord)
    assert [r.result for r in runs[2:]] == ["skipped-open-pr", "skipped-open-pr"]
    assert len(runner.inputs) == 2

    # 3) 사람이 코드 브랜치를 머지하고 다음 날 실행한다.
    code_branch = next(
        line.split("refs/heads/")[1]
        for line in branches.splitlines()
        if "refs/heads/sync/code-" in line
    )
    await data_work.run(["fetch", "-q", "origin"])
    await data_work.run(["merge", "-q", "--ff-only", "origin/main"])
    await data_work.run(
        ["merge", "-q", "--no-ff", "-m", f"merge {code_branch}", f"origin/{code_branch}"]
    )
    await data_work.run(["push", "-q", "origin", "main"])
    clock.value = START + timedelta(days=1)
    assert await run() == 0
    capsys.readouterr()
    reviews = read_jsonl(state / "pr-review.jsonl", PrReviewRecord)
    assert [(r.kind, r.outcome, r.human_changed_files) for r in reviews] == [("code", "merged", 0)]
    events = read_jsonl(state / "oq-events.jsonl", OqEventRecord)
    assert [(e.kind, e.title) for e in events] == [("opened", "[2026-10-07] 샘플 어긋남")]
    last = read_jsonl(state / "sync-runs.jsonl", SyncRunRecord)[4:]
    assert {r.kind: r.result for r in last} == {"code": "no-change", "ingest": "skipped-open-pr"}

    # 4) 봇이 읽는 사본을 맞춘다.
    assert await apull(env) == 0
    assert (home / "data" / "wiki" / "index.md").is_file()

    assert offline_guard.attempts == []
    assert requests == []
