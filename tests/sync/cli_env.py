"""진입점 시험 공용: 설정 파일·환경변수·가짜 `build_deps`를 한 번에 만든다."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from team_wiki_engine.config.load import Paths, Secrets
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.sync.stage import SyncDeps
from tests.helpers.sync_env import ENGINE_DIR, SyncEnv, make_sync_env

API_KEY = "sk-ant-api03-SECRETKEYVALUE0123456789"
GITHUB_TOKEN = "ghp_Zx81QmN4vT7bK2pLs9Wd3Yc6Hf0JrAeU5"


@dataclass
class Cli:
    sync: SyncEnv
    env: dict[str, str]
    config_path: Path

    def fake_build(self) -> Any:
        deps = self.sync.deps

        def build(
            config: TeamConfig,
            secrets: Secrets,
            paths: Paths,
            *,
            env: Any,
            http: httpx2.AsyncClient,
        ) -> SyncDeps:
            return deps

        return build


async def make_cli(root: Path, **kwargs: Any) -> Cli:
    sync = await make_sync_env(root, **kwargs)
    config = {
        "repos": [{"name": r.name, "url": r.url} for r in sync.config.repos],
        "dataRepo": {"url": sync.config.data_repo.url},
    }
    config_path = root / "team.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root / "git-home"),
        "TEAM_WIKI_HOME": str(sync.paths.home),
        "TEAM_WIKI_CONFIG": str(config_path),
        "TEAM_WIKI_ENGINE_DIR": str(ENGINE_DIR),
        "ANTHROPIC_API_KEY": API_KEY,
    }
    return Cli(sync=sync, env=env, config_path=config_path)
