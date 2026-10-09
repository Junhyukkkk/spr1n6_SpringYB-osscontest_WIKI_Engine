"""`team-wiki-bot` 진입점.

종료 코드: 0 신호로 정상 종료, 1 실행 오류(연결이 스스로 끝남, `data.lock`을 못 잡음),
2 사용·설정 오류. 표준 오류에는 비밀값·서버 경로를 싣지 않는다(`safe_message`).
로그 처리기는 `main()`에서만 붙인다(`amain`을 부르는 시험이 루트 로거를 더럽히지 않게).
`drain_background_tasks`는 `BotApp`이 정확히 한 번 부른다 — 여기서는 부르지 않는다.
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx2

from team_wiki_engine.bot.app import BotApp
from team_wiki_engine.bot.gateway import DiscordGateway, configure_discord_logging
from team_wiki_engine.bot.ports import Gateway
from team_wiki_engine.bot.wiring import bot_settings, build_bot_deps
from team_wiki_engine.config.load import ConfigError, load_config, load_secrets, resolve_paths
from team_wiki_engine.git.client import GitError, check_git_version
from team_wiki_engine.metrics.record import STATE_FILES
from team_wiki_engine.sync.lock import LockError, SyncLock, acquire_lock
from team_wiki_engine.sync.main import engine_dir_from, secret_values
from team_wiki_engine.sync.stage import safe_message

USAGE: Final = "사용법: team-wiki-bot"
DATA_LOCK_WAIT_S: Final = 30.0
DEFAULT_CONFIG: Final = "/etc/team-wiki/team.json"
LOCK_RETRY_S: Final = 1.0


def eprint(text: str) -> None:
    stream = sys.stderr
    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError, UnicodeError):
        stream.write(text + "\n")
        stream.flush()


async def acquire_data_lock(
    state_dir: Path, *, now: Callable[[], datetime], wait_s: float = DATA_LOCK_WAIT_S
) -> SyncLock | None:
    """`data.lock`을 1초 간격으로 `wait_s`까지 다시 시도한다. `LockError`는 그대로 올린다."""
    deadline = time.monotonic() + wait_s
    while True:
        lock = acquire_lock(state_dir / STATE_FILES.data_lock, now=now())
        if lock is not None:
            return lock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(LOCK_RETRY_S, remaining))


def _default_http() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=httpx2.Timeout(30.0), follow_redirects=False, trust_env=False)


async def amain(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    gateway_factory: Callable[[str], Gateway] = DiscordGateway,
    http_factory: Callable[[], httpx2.AsyncClient] | None = None,
    build_deps: Callable[..., object] = build_bot_deps,
    stop: asyncio.Event | None = None,
) -> int:
    redact = secret_values(env)
    if list(argv):
        eprint(USAGE)
        return 2
    try:
        engine_dir = engine_dir_from(env)
        if not (engine_dir / "skills").is_dir():
            raise ConfigError("엔진 폴더에 skills/가 없습니다 (TEAM_WIKI_ENGINE_DIR 확인)")
        await check_git_version(base_env=env)
        config = load_config(env.get("TEAM_WIKI_CONFIG", "").strip() or DEFAULT_CONFIG)
        secrets = load_secrets(env, config, "bot")
        paths = resolve_paths(env, engine_dir)
        bot_settings(config)
    except (ConfigError, GitError) as exc:
        eprint(safe_message(str(exc) or type(exc).__name__, redact))
        return 2
    token = secrets.discord_token
    if token is None:
        eprint("DISCORD_TOKEN이 필요합니다")
        return 2

    try:
        lock = await acquire_data_lock(
            paths.state_dir, now=lambda: datetime.now(UTC), wait_s=DATA_LOCK_WAIT_S
        )
    except LockError:
        eprint("data.lock을 열 수 없습니다")
        return 2
    if lock is None:
        eprint("다른 봇이나 사본 갱신이 data 사본을 쓰고 있어 시작하지 않습니다")
        return 1

    loop = asyncio.get_running_loop()
    stop_event = stop or asyncio.Event()
    installed: list[signal.Signals] = []
    try:
        async with (http_factory or _default_http)() as http:
            try:
                deps = build_deps(
                    config, secrets, paths, env=env, http=http, gateway=gateway_factory(token)
                )
            except ConfigError as exc:
                eprint(safe_message(str(exc) or type(exc).__name__, redact))
                return 2
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, stop_event.set)
                except (NotImplementedError, RuntimeError, ValueError):
                    continue
                installed.append(sig)
            try:
                return await BotApp(deps).run(stop_event)  # type: ignore[arg-type]
            except Exception as exc:
                eprint(f"[bot] 실행 오류: {type(exc).__name__}")
                return 1
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)
        lock.release()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    configure_discord_logging()
    args = list(sys.argv[1:] if argv is None else argv)
    return asyncio.run(amain(args, dict(os.environ)))
