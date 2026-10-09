"""봇 설정 검증과 의존성 조립. 오류는 모두 `ConfigError`이고 메시지에 비밀값·ID 값을 싣지 않는다."""

import os
import time
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx2

from team_wiki_engine.bot.answer import AnswerDeps, AnswerPipeline
from team_wiki_engine.bot.app import BotDeps
from team_wiki_engine.bot.data_gate import DataGate
from team_wiki_engine.bot.dispatch import (
    MAX_PENDING_REPLIES,
    BotSettings,
    Dispatcher,
    InFlight,
    PauseSwitch,
)
from team_wiki_engine.bot.limiter import Limiter
from team_wiki_engine.bot.ports import Gateway
from team_wiki_engine.bot.refresh import DataRefresher
from team_wiki_engine.bot.salt import load_or_create_user_salt
from team_wiki_engine.bot.sessions import SessionStore
from team_wiki_engine.bot.text import is_snowflake
from team_wiki_engine.claude.runner import create_claude_runner
from team_wiki_engine.claude.types import ClaudeRunner
from team_wiki_engine.config.load import ConfigError, Paths, Secrets
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.git.client import GitClient, auth_header_for
from team_wiki_engine.git.ops import is_safe_branch
from team_wiki_engine.github.client import parse_repo_url
from team_wiki_engine.metrics.record import STATE_FILES, QuestionRecord, read_jsonl
from team_wiki_engine.ops.notify import DiscordNotifier
from team_wiki_engine.sync.data_copy import prepare_data_copy
from team_wiki_engine.sync.stage import Mask, path_roots
from team_wiki_engine.sync.wiring import runner_deps_from_config
from team_wiki_engine.util.time import kst_parts, parse_instant


def bot_settings(config: TeamConfig) -> BotSettings:
    d = config.discord
    if (
        not is_snowflake(d.guild_id)
        or not d.channel_ids
        or not all(is_snowflake(c) for c in d.channel_ids)
    ):
        raise ConfigError(
            "discord.guildId와 discord.channelIds를 숫자 ID로 채워야 봇을 시작할 수 있습니다"
        )
    optional: list[str | None] = []
    for name, value in (("adminUserId", d.admin_user_id), ("opsChannelId", d.ops_channel_id)):
        if value == "":
            optional.append(None)
        elif is_snowflake(value):
            optional.append(value)
        else:
            raise ConfigError(f"discord.{name}는 숫자 ID여야 합니다")
    return BotSettings(
        guild_id=d.guild_id,
        channel_ids=frozenset(d.channel_ids),
        admin_user_id=optional[0],
        ops_channel_id=optional[1],
    )


def _is_local_path(url: str) -> bool:
    return (
        os.path.isabs(url) and "://" not in url and not url.startswith("git@") and url.isprintable()
    )


def data_git_for(
    config: TeamConfig, secrets: Secrets, paths: Paths, env: Mapping[str, str]
) -> GitClient:
    if not is_safe_branch(config.data_repo.branch):
        raise ConfigError("dataRepo.branch가 안전한 브랜치 이름이 아닙니다")
    url = config.data_repo.url
    if config.delivery == "local":
        if not _is_local_path(url):
            raise ConfigError("delivery: local에서는 dataRepo.url이 로컬 절대 경로여야 합니다")
        return GitClient(paths.data_dir, allowed_protocols="file", base_env=env)
    if parse_repo_url(url) is None:
        raise ConfigError("delivery: github에서는 dataRepo.url이 GitHub 주소여야 합니다")
    return GitClient(
        paths.data_dir,
        auth_header=auth_header_for(secrets.github_token) if secrets.github_token else None,
        allowed_protocols="https",
        base_env=env,
    )


def today_usage(state_dir: Path, now: datetime) -> tuple[int, float]:
    today = kst_parts(now).date
    count = 0
    cost = 0.0
    for record in read_jsonl(state_dir / STATE_FILES.questions, QuestionRecord, tail=True):
        moment = parse_instant(record.at)
        if moment is not None and kst_parts(moment).date == today:
            count += 1
            cost += record.cost_usd
    return count, cost


def build_bot_deps(
    config: TeamConfig,
    secrets: Secrets,
    paths: Paths,
    *,
    env: Mapping[str, str],
    http: httpx2.AsyncClient,
    gateway: Gateway,
    runner: ClaudeRunner | None = None,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> BotDeps:
    for path in (paths.home, paths.engine_dir):
        if not unicodedata.is_normalized("NFC", str(path)):
            raise ConfigError("작업 폴더 경로가 NFC가 아닙니다")
    settings = bot_settings(config)
    data_git = data_git_for(config, secrets, paths, env)
    clock: Callable[[], datetime] = now if now is not None else (lambda: datetime.now(UTC))
    mono: Callable[[], float] = monotonic if monotonic is not None else time.monotonic
    redact = tuple(
        v for v in (secrets.anthropic_api_key, secrets.discord_token, secrets.github_token) if v
    )
    user_salt = load_or_create_user_salt(paths.state_dir)
    the_runner = runner or create_claude_runner(runner_deps_from_config(config, paths, env))
    notifier = DiscordNotifier(
        token=secrets.discord_token,
        channel_id=settings.ops_channel_id or "",
        admin_user_id=settings.admin_user_id or "",
        client=http,
        redact=redact,
    )
    mask = Mask(redact, path_roots(paths))
    limiter = Limiter.from_limits(config.limits, now=clock, monotonic=mono)
    sessions = SessionStore(paths.state_dir / STATE_FILES.sessions, now=clock)
    gate = DataGate(monotonic=mono)
    pipeline = AnswerPipeline(
        AnswerDeps(
            runner=the_runner,
            sessions=sessions,
            limiter=limiter,
            gate=gate,
            paths=paths,
            config=config,
            notifier=notifier,
            now=clock,
            monotonic=mono,
            redact=redact,
            user_salt=user_salt,
        )
    )
    count, cost = today_usage(paths.state_dir, clock())
    limiter.seed_daily(count)
    pipeline.seed_cost_today(cost)
    url, branch = config.data_repo.url, config.data_repo.branch
    refresher = DataRefresher(
        refresh=lambda: prepare_data_copy(data_git, url, branch),
        gate=gate,
        data_dir=paths.data_dir,
        notifier=notifier,
        mask=mask,
        now=clock,
    )
    pause = PauseSwitch(paths.state_dir / STATE_FILES.bot_control, now=clock)
    in_flight = InFlight(config.limits.concurrent + MAX_PENDING_REPLIES)
    dispatcher = Dispatcher(
        settings=settings,
        pipeline=pipeline,
        limiter=limiter,
        pause=pause,
        in_flight=in_flight,
        refresh_state=lambda: refresher.state,
        notifier=notifier,
        user_salt=user_salt,
    )
    return BotDeps(
        pipeline=pipeline,
        dispatcher=dispatcher,
        refresher=refresher,
        in_flight=in_flight,
        notifier=notifier,
        gateway=gateway,
        env=dict(env),
    )


__all__ = [
    "bot_settings",
    "build_bot_deps",
    "data_git_for",
    "runner_deps_from_config",
    "today_usage",
]
