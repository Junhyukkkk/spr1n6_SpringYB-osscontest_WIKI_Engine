"""동기화 의존성 조립(`build_sync_deps`)과 Claude 실행기 설정(`runner_deps_from_config`).

검증 순서(G12): ① `httpx2` 클라이언트 설정 ② 작업 폴더 NFC ③ 브랜치 이름 ④ 데이터 저장소 주소와
토큰 — **모두 통과한 뒤에** 객체(`GitHubRest`, `GitClient`, 러너, 알림기)를 만든다. 검증이 하나라도
실패하면 어떤 객체도 만들지 않고 폴더도 건드리지 않는다. 오류는 모두 `ConfigError`이며 메시지에
비밀값·주소의 자격 증명·환경변수 값을 싣지 않는다.

`http`는 호출자(`main`)가 `httpx2.AsyncClient(trust_env=False, follow_redirects=False)`로 만들고
닫는다. 환경변수의 프록시 설정이 끼어들거나 리다이렉트로 인증 헤더가 다른 곳에 가면 안 되므로
다르게 만들어진 클라이언트는 거부한다. `SyncDeps`는 환경변수 전체나 토큰을 필드로 들지 않는다
(`redact`는 `repr=False`).
"""

import dataclasses
import os
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

import httpx2

from team_wiki_engine.claude.runner import RunnerDeps, create_claude_runner
from team_wiki_engine.config.load import ConfigError, Paths, Secrets
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.git.client import GitClient, auth_header_for
from team_wiki_engine.git.ops import is_safe_branch
from team_wiki_engine.github.client import GitHubRest, RepoRef, parse_repo_url
from team_wiki_engine.ops.notify import DiscordNotifier
from team_wiki_engine.sync.pr_port import GitHubPrPort, LocalPrPort, PrPort
from team_wiki_engine.sync.stage import SyncDeps


def runner_deps_from_config(config: TeamConfig, paths: Paths, env: Mapping[str, str]) -> RunnerDeps:
    """설정·작업 폴더·환경변수로 Claude 실행기 의존성을 만든다(G3, 이월 3).

    `effort`는 `EffortConfig` 그대로, `env`는 복사본이다(`RunnerDeps.env`는 `repr`에 나오지 않는다).
    """
    return RunnerDeps(
        model=config.model,
        effort=config.effort,
        skills_dir=paths.skills_dir,
        env=dict(env),
    )


def _check_http(http: httpx2.AsyncClient) -> None:
    if http.trust_env:
        raise ConfigError("httpx2 클라이언트는 trust_env=False로 만들어야 합니다")
    if http.follow_redirects:
        raise ConfigError("httpx2 클라이언트는 follow_redirects=False로 만들어야 합니다")


def _check_nfc(paths: Paths) -> None:
    """작업 폴더 경로는 NFC여야 한다(경로 가드가 비-NFC를 거부하고 리눅스는 NFC로 못 연다)."""
    for field in dataclasses.fields(paths):
        if not unicodedata.is_normalized("NFC", str(getattr(paths, field.name))):
            raise ConfigError(f"작업 폴더 경로가 NFC가 아닙니다: {field.name}")


def _check_branches(config: TeamConfig) -> None:
    if not is_safe_branch(config.data_repo.branch):
        raise ConfigError("dataRepo.branch가 안전한 브랜치 이름이 아닙니다")
    for repo in config.repos:
        if not is_safe_branch(repo.branch):
            raise ConfigError(f"repos[{repo.name}].branch가 안전한 브랜치 이름이 아닙니다")


def _is_local_path(url: str) -> bool:
    return (
        os.path.isabs(url) and "://" not in url and not url.startswith("git@") and url.isprintable()
    )


def _check_data_repo(config: TeamConfig, secrets: Secrets) -> tuple[RepoRef, str] | None:
    """데이터 저장소 주소를 배달 방식에 맞게 확인한다. github면 (저장소 참조, 토큰)을 돌려준다."""
    url = config.data_repo.url
    if config.delivery == "local":
        if not _is_local_path(url):
            raise ConfigError("delivery: local에서는 dataRepo.url이 로컬 절대 경로여야 합니다")
        return None
    repo = parse_repo_url(url)
    if repo is None:
        # 주소에 자격 증명이 섞여 있을 수 있어 값을 싣지 않는다.
        raise ConfigError("delivery: github에서는 dataRepo.url이 GitHub 주소여야 합니다")
    token = secrets.github_token
    if not token:
        raise ConfigError("delivery: github에는 GITHUB_TOKEN이 필요합니다")
    return repo, token


def build_sync_deps(
    config: TeamConfig,
    secrets: Secrets,
    paths: Paths,
    *,
    env: Mapping[str, str],
    http: httpx2.AsyncClient,
    now: Callable[[], datetime] | None = None,
    log: Callable[[str], None] | None = None,
) -> SyncDeps:
    # 1) 검증 — 객체를 만들기 전에 모두 끝낸다.
    _check_http(http)
    _check_nfc(paths)
    _check_branches(config)
    github_data = _check_data_repo(config, secrets)

    # 2) 조립.
    clock: Callable[[], datetime] = now if now is not None else (lambda: datetime.now(UTC))
    try:
        github = GitHubRest(token=secrets.github_token, client=http)
    except ValueError:
        raise ConfigError("GitHub 토큰 형식이 올바르지 않습니다") from None

    if (
        github_data is None
    ):  # delivery: local — 인증 없음, file 전송만(실수로 GitHub에 push 못 하게)
        data_git = GitClient(paths.data_sync_dir, allowed_protocols="file", base_env=env)
    else:
        data_git = GitClient(
            paths.data_sync_dir,
            auth_header=auth_header_for(github_data[1]),
            allowed_protocols="https",
            base_env=env,
        )
    # 비공개 코드 저장소도 받을 수 있게 토큰이 있으면 인증 헤더를 쓴다. 헤더는 환경변수로만 넘기고
    # `https://github.com/` 주소에만 붙으므로(GitClient) 다른 호스트로는 나가지 않는다.
    code_git = GitClient(
        paths.repos_dir,
        auth_header=auth_header_for(secrets.github_token) if secrets.github_token else None,
        base_env=env,
    )

    pr_port: PrPort | None = None
    if config.review_mode == "pr":
        if github_data is None:
            pr_port = LocalPrPort(
                directory=paths.local_prs_dir,
                data_git=data_git,
                base=config.data_repo.branch,
                now=clock,
            )
        else:
            pr_port = GitHubPrPort(github=github, repo=github_data[0], data_git=data_git)

    redact = tuple(
        value
        for value in (secrets.anthropic_api_key, secrets.discord_token, secrets.github_token)
        if value
    )
    runner = create_claude_runner(runner_deps_from_config(config, paths, env))
    notifier = DiscordNotifier(
        token=secrets.discord_token,
        channel_id=config.discord.ops_channel_id,
        admin_user_id=config.discord.admin_user_id,
        client=http,
        log=log,
        redact=redact,
    )
    return SyncDeps(
        config=config,
        paths=paths,
        runner=runner,
        github=github,
        pr_port=pr_port,
        data_git=data_git,
        code_git=code_git,
        notifier=notifier,
        now=clock,
        redact=redact,
    )
