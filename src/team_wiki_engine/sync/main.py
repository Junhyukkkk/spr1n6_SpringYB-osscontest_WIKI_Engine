"""`team-wiki-sync [--only code|ingest]` 진입점.

종료 코드: 0 성공(잠금이 바빠 건너뛴 것 포함), 1 단계 실패·실행 오류·취소, 2 사용·설정 오류.

stderr에는 비밀값·서버 경로·옵션 객체를 싣지 않는다. 모든 글은 `safe_message`를 지난다.

신호(G19): `SIGTERM`·`SIGINT`는 메인 태스크를 취소한다. 처리기는 실행 중인 루프에 달고 `finally`에서
**항상** 뗀다. 정리 대기(`drain_background_tasks`)는 의존성이 만들어진 뒤의 `finally`에서 정확히
한 번 부른다. 그 전(인자·설정·git 버전·`build_deps` 실패)에는 Claude를 부른 적이 없으므로 기다릴
것이 없다. 로그 처리기는 `main()`에서만 붙인다(`amain`을 부르는 시험이 루트 로거를 더럽히지 않게).
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import httpx2

from team_wiki_engine.claude.runner import drain_background_tasks
from team_wiki_engine.config.load import ConfigError, load_config, load_secrets, resolve_paths
from team_wiki_engine.git.client import GitError, check_git_version
from team_wiki_engine.sync.run import run_once
from team_wiki_engine.sync.stage import SyncDeps, describe_error, make_mask, safe_message
from team_wiki_engine.sync.types import SyncKind
from team_wiki_engine.sync.wiring import build_sync_deps

USAGE: Final = "사용법: team-wiki-sync [--only code|ingest]"
DEFAULT_CONFIG: Final = "/etc/team-wiki/team.json"
_SECRET_KEYS: Final = ("ANTHROPIC_API_KEY", "DISCORD_TOKEN", "GITHUB_TOKEN")


class UsageError(Exception):
    """명령줄 인자가 올바르지 않다."""


def parse_args(argv: Sequence[str]) -> SyncKind | None:
    args = list(argv)
    if not args:
        return None
    if len(args) == 2 and args[0] == "--only":
        if args[1] == "code":
            return "code"
        if args[1] == "ingest":
            return "ingest"
    raise UsageError(USAGE)


def engine_dir_from(env: Mapping[str, str]) -> Path:
    """`TEAM_WIKI_ENGINE_DIR`(공백 제거, 절대 경로)이나 패키지 위치의 저장소 루트."""
    configured = env.get("TEAM_WIKI_ENGINE_DIR", "").strip()
    if not configured:
        return Path(__file__).resolve().parents[3]
    if not os.path.isabs(configured):
        raise ConfigError("TEAM_WIKI_ENGINE_DIR는 절대 경로여야 합니다")
    return Path(configured)


def secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    """환경변수의 알려진 비밀값(가릴 대상)."""
    return tuple(value for key in _SECRET_KEYS if (value := env.get(key, "").strip()))


def eprint(text: str) -> None:
    """stderr에 한 줄. 닫혀 있거나 인코딩할 수 없어도 던지지 않는다."""
    stream = sys.stderr
    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError, UnicodeError):
        stream.write(text + "\n")
        stream.flush()


def _default_http() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=httpx2.Timeout(30.0), follow_redirects=False, trust_env=False)


def _install_signals(loop: asyncio.AbstractEventLoop, cancel: Callable[[], None]) -> list[int]:
    installed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, cancel)
        except (NotImplementedError, RuntimeError, ValueError):
            continue  # 주 스레드가 아니거나 지원하지 않는 플랫폼
        installed.append(sig)
    return installed


def _remove_signals(loop: asyncio.AbstractEventLoop, installed: Sequence[int]) -> None:
    for sig in installed:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(sig)


async def _drain_quietly(timeout_s: float, main_task: asyncio.Task[Any] | None) -> bool:
    """남은 백그라운드 작업을 기다린다. 기다리다 취소되면(두 번째 신호) `False`."""
    try:
        with contextlib.suppress(Exception):
            await drain_background_tasks(timeout_s)
    except asyncio.CancelledError:
        if main_task is not None:
            main_task.uncancel()
        return False
    return True


async def amain(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    build_deps: Callable[..., SyncDeps] = build_sync_deps,
    http_factory: Callable[[], httpx2.AsyncClient] | None = None,
) -> int:
    redact = secret_values(env)
    try:
        only = parse_args(argv)
    except UsageError:
        eprint(USAGE)
        return 2
    try:
        engine_dir = engine_dir_from(env)
        if not (engine_dir / "skills").is_dir():
            raise ConfigError("엔진 폴더에 skills/가 없습니다 (TEAM_WIKI_ENGINE_DIR 확인)")
        await check_git_version(base_env=env)
        config = load_config(env.get("TEAM_WIKI_CONFIG", "").strip() or DEFAULT_CONFIG)
        secrets = load_secrets(env, config, "sync")
        paths = resolve_paths(env, engine_dir)
    except (ConfigError, GitError) as exc:
        eprint(safe_message(str(exc) or type(exc).__name__, redact))
        return 2

    async with (http_factory or _default_http)() as http:
        try:
            deps = build_deps(config, secrets, paths, env=env, http=http)
        except ConfigError as exc:
            eprint(safe_message(str(exc) or type(exc).__name__, redact))
            return 2
        except Exception as exc:  # noqa: BLE001 - 예상 못 한 조립 실패도 스택 없이 끝낸다
            eprint(safe_message(f"실행 준비 실패: {type(exc).__name__}", redact))
            return 1

        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()
        by_signal = False

        def on_signal() -> None:
            nonlocal by_signal
            by_signal = True
            if main_task is not None:
                main_task.cancel()

        installed = _install_signals(loop, on_signal)
        try:
            try:
                report = await run_once(deps, only=only)
            except asyncio.CancelledError:
                if not by_signal:
                    raise
                if main_task is not None:
                    main_task.uncancel()
                return 1
            except Exception as exc:
                mask = make_mask(deps)
                text = "[sync] 실패: " + safe_message(
                    describe_error(exc, mask) or type(exc).__name__, deps.redact, roots=mask.roots
                )
                eprint(text)
                with contextlib.suppress(Exception):
                    await deps.notifier.send(text, mention_admin=True)
                return 1
            if report is None:
                return 0
            return 1 if any(stage.result == "failed" for stage in report.stages) else 0
        finally:
            _remove_signals(loop, installed)
            if not await _drain_quietly(deps.settle_timeout_s, main_task):
                return 1  # noqa: B012 - 정리 중 두 번째 신호는 트레이스백 없이 끝낸다


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    args = list(sys.argv[1:] if argv is None else argv)
    return asyncio.run(amain(args, dict(os.environ)))
