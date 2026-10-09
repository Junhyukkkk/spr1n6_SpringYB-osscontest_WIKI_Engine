"""`team-wiki-pull`: 봇이 읽는 데이터 저장소 사본(`data/`)을 원격과 똑같이 맞춘다.

Claude 키는 필요 없다(`load_secrets`를 쓰지 않는다). `delivery`가 `github`면 `https`만 허용하고
`GITHUB_TOKEN`이 있으면 환경변수 인증 헤더를 쓴다. 로컬이면 `file`만 허용한다.

종료 코드: 0 성공, 1 git·사본 실패, 2 사용·설정 오류. stderr 글은 `safe_message`로 가린다.
"""

import asyncio
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

from team_wiki_engine.config.load import ConfigError, load_config, resolve_paths
from team_wiki_engine.git.client import GitClient, GitError, auth_header_for, check_git_version
from team_wiki_engine.git.ops import CloneStateError
from team_wiki_engine.metrics.record import STATE_FILES
from team_wiki_engine.sync.data_copy import prepare_data_copy
from team_wiki_engine.sync.lock import LockError, acquire_lock
from team_wiki_engine.sync.main import DEFAULT_CONFIG, engine_dir_from, eprint, secret_values
from team_wiki_engine.sync.stage import path_roots, safe_message

USAGE: Final = "사용법: team-wiki-pull"
PULL_SKIPPED_MESSAGE: Final = "[pull] 봇이 data 사본을 관리하고 있어 건너뜀"


async def apull(env: Mapping[str, str]) -> int:
    redact = secret_values(env)
    try:
        engine_dir = engine_dir_from(env)
        await check_git_version(base_env=env)
        config = load_config(env.get("TEAM_WIKI_CONFIG", "").strip() or DEFAULT_CONFIG)
        paths = resolve_paths(env, engine_dir)
    except (ConfigError, GitError) as exc:
        eprint(safe_message(str(exc) or type(exc).__name__, redact))
        return 2

    token = env.get("GITHUB_TOKEN", "").strip()
    if config.delivery == "github":
        git = GitClient(
            paths.data_dir,
            auth_header=auth_header_for(token) if token else None,
            allowed_protocols="https",
            base_env=env,
        )
    else:
        git = GitClient(paths.data_dir, allowed_protocols="file", base_env=env)
    try:
        lock = acquire_lock(paths.state_dir / STATE_FILES.data_lock, now=datetime.now(UTC))
    except LockError:
        eprint("[pull] data.lock을 열 수 없습니다")
        return 2
    if lock is None:  # 봇이 사본을 쥐고 있다 — 건드리지 않는다
        print(PULL_SKIPPED_MESSAGE, flush=True)
        return 0
    try:
        await prepare_data_copy(git, config.data_repo.url, config.data_repo.branch)
    except (GitError, CloneStateError, ValueError, OSError) as exc:
        text = type(exc).__name__ if isinstance(exc, OSError) else str(exc)
        eprint(
            "데이터 사본 갱신 실패: "
            + safe_message(text or type(exc).__name__, redact, roots=path_roots(paths))
        )
        return 1
    finally:
        lock.release()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        eprint(USAGE)
        return 2
    return asyncio.run(apull(dict(os.environ)))
