"""의존성 조립(`build_sync_deps`·`runner_deps_from_config`) 시험(Task 16)."""

import json
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from team_wiki_engine.claude.runner import RunnerDeps
from team_wiki_engine.config.load import ConfigError, Paths, Secrets, parse_config, resolve_paths
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.git.client import GitClient
from team_wiki_engine.github.client import GitHubRest
from team_wiki_engine.sync import wiring
from team_wiki_engine.sync.pr_port import GitHubPrPort, LocalPrPort
from team_wiki_engine.sync.stage import SyncDeps
from team_wiki_engine.sync.wiring import build_sync_deps, runner_deps_from_config
from tests.helpers.http import idle_client, mock_client
from tests.helpers.sync_env import ENGINE_DIR

ANTHROPIC_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
# 가짜 값. 한 문자열로 쓰면 GitHub push 보호가 진짜 디스코드 토큰으로 보고 push를 막는다.
DISCORD_TOKEN = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4", "GhIjKl", "AbCdEfGhIjKlMnOpQrStUvWxYz012345"])
GITHUB_TOKEN = "ghp_Zx81QmN4vT7bK2pLs9Wd3Yc6Hf0JrAeU5"
SECRETS = Secrets(
    anthropic_api_key=ANTHROPIC_KEY, discord_token=DISCORD_TOKEN, github_token=GITHUB_TOKEN
)
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/home/x", "ANTHROPIC_API_KEY": ANTHROPIC_KEY}
GITHUB_DATA_URL = "https://github.com/me/team-wiki-data.git"
CHANNEL = "123456789012345678"
ADMIN = "223456789012345678"


def config_of(**overrides: Any) -> TeamConfig:
    raw: dict[str, Any] = {
        "repos": [{"name": "worker", "url": "https://github.com/o/worker.git"}],
        "dataRepo": {"url": "/tmp/data.git"},
        "discord": {"opsChannelId": CHANNEL, "adminUserId": ADMIN},
        **overrides,
    }
    return parse_config(raw)


def github_config(**overrides: Any) -> TeamConfig:
    return config_of(**{"delivery": "github", "dataRepo": {"url": GITHUB_DATA_URL}, **overrides})


def paths_in(root: Path) -> Paths:
    return resolve_paths({"TEAM_WIKI_HOME": str(root / "home")}, ENGINE_DIR)


class RecordingGit(GitClient):
    """생성 인자를 기록하는 `GitClient`."""

    created: list[dict[str, Any]] = []

    def __init__(self, cwd: Path, **kwargs: Any) -> None:
        RecordingGit.created.append({"cwd": cwd, **kwargs})
        super().__init__(cwd, **kwargs)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    RecordingGit.created = []
    monkeypatch.setattr(wiring, "GitClient", RecordingGit)
    return RecordingGit.created


def build(
    config: TeamConfig,
    root: Path,
    *,
    http: httpx2.AsyncClient | None = None,
    **kwargs: Any,
) -> SyncDeps:
    return build_sync_deps(
        config, SECRETS, paths_in(root), env=ENV, http=http or idle_client(), **kwargs
    )


# ---------------------------------------------------------------- runner_deps_from_config


class TestRunnerDepsFromConfig:
    def test_설정과_작업_폴더_환경을_그대로_옮긴다(self, tmp_dir: Path) -> None:
        config = config_of()
        paths = paths_in(tmp_dir)
        deps = runner_deps_from_config(config, paths, ENV)
        assert isinstance(deps, RunnerDeps)
        assert deps.model == config.model
        assert deps.effort is config.effort
        assert deps.skills_dir == paths.skills_dir
        assert dict(deps.env) == ENV

    def test_환경은_복사본이다(self, tmp_dir: Path) -> None:
        env = dict(ENV)
        deps = runner_deps_from_config(config_of(), paths_in(tmp_dir), env)
        env["ANTHROPIC_API_KEY"] = "바뀐 값"
        env["EXTRA"] = "x"
        assert deps.env["ANTHROPIC_API_KEY"] == ANTHROPIC_KEY
        assert "EXTRA" not in deps.env

    def test_repr에_환경_값이_없다(self, tmp_dir: Path) -> None:
        deps = runner_deps_from_config(config_of(), paths_in(tmp_dir), ENV)
        assert ANTHROPIC_KEY not in repr(deps)
        assert ANTHROPIC_KEY not in str(deps)


# ---------------------------------------------------------------- 분기


class TestBranches:
    def test_local은_로컬_PR_포트를_쓰고_인증이_없다(
        self, tmp_dir: Path, recorded: list[dict[str, Any]]
    ) -> None:
        deps = build(config_of(), tmp_dir)
        assert isinstance(deps.pr_port, LocalPrPort)
        assert deps.data_git.has_auth is False
        assert deps.github is not None
        assert isinstance(deps.github, GitHubRest)

    def test_local의_데이터_사본_git은_file만_허용하고_인증이_없다(
        self, tmp_dir: Path, recorded: list[dict[str, Any]]
    ) -> None:
        paths = paths_in(tmp_dir)
        deps = build(config_of(), tmp_dir)
        data_calls = [c for c in recorded if c["cwd"] == paths.data_sync_dir]
        assert len(data_calls) == 1
        assert data_calls[0].get("allowed_protocols") == "file"
        assert data_calls[0].get("auth_header") is None
        assert data_calls[0].get("base_env") == ENV
        assert deps.data_git.has_auth is False

    def test_코드_사본_git은_토큰이_있으면_인증하고_기본_전송이다(
        self, tmp_dir: Path, recorded: list[dict[str, Any]]
    ) -> None:
        paths = paths_in(tmp_dir)
        deps = build(github_config(), tmp_dir)
        code_calls = [c for c in recorded if c["cwd"] == paths.repos_dir]
        assert len(code_calls) == 1
        assert code_calls[0].get("auth_header") is not None
        assert "allowed_protocols" not in code_calls[0]  # 기본 `https:file`
        assert code_calls[0].get("base_env") == ENV
        assert deps.code_git.has_auth is True
        assert deps.code_git.cwd == paths.repos_dir
        assert GITHUB_TOKEN not in repr(deps.code_git)

    def test_토큰이_없으면_코드_사본_git은_인증하지_않는다(self, tmp_dir: Path) -> None:
        no_token = Secrets(anthropic_api_key=ANTHROPIC_KEY, discord_token=None, github_token="")
        deps = build_sync_deps(
            config_of(), no_token, paths_in(tmp_dir), env=ENV, http=idle_client()
        )
        assert deps.code_git.has_auth is False

    def test_direct_모드는_PR_포트가_없다(self, tmp_dir: Path) -> None:
        deps = build(config_of(reviewMode="direct"), tmp_dir)
        assert deps.pr_port is None

    def test_github_모드는_인증_git과_GitHub_PR_포트를_만든다(
        self, tmp_dir: Path, recorded: list[dict[str, Any]]
    ) -> None:
        paths = paths_in(tmp_dir)
        deps = build(github_config(), tmp_dir)
        assert deps.data_git.has_auth is True
        assert isinstance(deps.pr_port, GitHubPrPort)
        data_calls = [c for c in recorded if c["cwd"] == paths.data_sync_dir]
        assert data_calls[0].get("allowed_protocols") == "https"
        assert data_calls[0].get("base_env") == ENV
        assert deps.code_git.has_auth is True

    def test_github_direct_모드는_인증은_있고_포트가_없다(self, tmp_dir: Path) -> None:
        deps = build(github_config(reviewMode="direct"), tmp_dir)
        assert deps.data_git.has_auth is True
        assert deps.pr_port is None

    def test_러너와_알림기를_만들고_기본값을_쓴다(self, tmp_dir: Path) -> None:
        deps = build(config_of(), tmp_dir)
        assert callable(deps.runner)
        assert deps.notifier is not None
        assert deps.config.model == "claude-sonnet-5-5"
        assert deps.settle_timeout_s == 30.0

    def test_now_기본값은_UTC_현재_시각이고_넘기면_그것을_쓴다(self, tmp_dir: Path) -> None:
        default = build(config_of(), tmp_dir).now()
        assert default.tzinfo is not None
        assert default.utcoffset() == UTC.utcoffset(None)
        fixed = datetime(2026, 1, 1, tzinfo=UTC)
        assert build(config_of(), tmp_dir, now=lambda: fixed).now() == fixed

    def test_redact는_비어_있지_않은_비밀값만(self, tmp_dir: Path) -> None:
        deps = build(config_of(), tmp_dir)
        assert set(deps.redact) == {ANTHROPIC_KEY, DISCORD_TOKEN, GITHUB_TOKEN}
        partial = Secrets(anthropic_api_key=ANTHROPIC_KEY, discord_token=None, github_token="")
        deps2 = build_sync_deps(
            config_of(), partial, paths_in(tmp_dir), env=ENV, http=idle_client()
        )
        assert deps2.redact == (ANTHROPIC_KEY,)

    def test_로컬_PR_포트는_설정의_기준_브랜치와_폴더를_쓴다(self, tmp_dir: Path) -> None:
        paths = paths_in(tmp_dir)
        deps = build(config_of(dataRepo={"url": "/tmp/data.git", "branch": "develop"}), tmp_dir)
        port = deps.pr_port
        assert isinstance(port, LocalPrPort)
        assert port._base == "develop"  # noqa: SLF001
        assert str(port._dir) == str(paths.local_prs_dir)  # noqa: SLF001


# ---------------------------------------------------------------- 검증(G12)


class TestValidation:
    def test_local에서_GitHub_주소는_ConfigError(self, tmp_dir: Path) -> None:
        with pytest.raises(
            ConfigError, match="delivery: local에서는 dataRepo.url이 로컬 절대 경로"
        ):
            build(config_of(dataRepo={"url": "https://github.com/me/data.git"}), tmp_dir)

    @pytest.mark.parametrize(
        "url",
        ["git@github.com:me/data.git", "relative/data.git", "ssh://host/data.git", "/tmp/a\nb"],
    )
    def test_local에서_절대_경로가_아니면_ConfigError(self, tmp_dir: Path, url: str) -> None:
        with pytest.raises(ConfigError):
            build(config_of(dataRepo={"url": url}), tmp_dir)

    def test_github에서_GitHub_주소가_아니면_ConfigError(self, tmp_dir: Path) -> None:
        with pytest.raises(ConfigError, match="GitHub"):
            build(github_config(dataRepo={"url": "/tmp/data.git"}), tmp_dir)

    def test_오류_메시지에_주소의_자격_증명이_없다(self, tmp_dir: Path) -> None:
        with pytest.raises(ConfigError) as info:
            build(
                github_config(dataRepo={"url": f"https://me:{GITHUB_TOKEN}@gitlab.com/x/y"}),
                tmp_dir,
            )
        assert GITHUB_TOKEN not in str(info.value)

    def test_github인데_토큰이_없으면_ConfigError(self, tmp_dir: Path) -> None:
        secrets = Secrets(ANTHROPIC_KEY, DISCORD_TOKEN, None)
        with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
            build_sync_deps(
                github_config(), secrets, paths_in(tmp_dir), env=ENV, http=idle_client()
            )

    def test_이상한_GitHub_토큰은_ConfigError이고_값을_싣지_않는다(self, tmp_dir: Path) -> None:
        bad = "ghp_와 한글 토큰"
        secrets = Secrets(ANTHROPIC_KEY, DISCORD_TOKEN, bad)
        with pytest.raises(ConfigError) as info:
            build_sync_deps(config_of(), secrets, paths_in(tmp_dir), env=ENV, http=idle_client())
        assert bad not in str(info.value)

    def test_NFD_작업_폴더는_ConfigError(self, tmp_dir: Path) -> None:
        nfd = unicodedata.normalize("NFD", "한글홈")
        assert nfd != unicodedata.normalize("NFC", nfd)
        paths = resolve_paths({"TEAM_WIKI_HOME": str(tmp_dir / nfd)}, ENGINE_DIR)
        with pytest.raises(ConfigError, match="NFC"):
            build_sync_deps(config_of(), SECRETS, paths, env=ENV, http=idle_client())

    def test_NFD_엔진_폴더도_ConfigError(self, tmp_dir: Path) -> None:
        nfd = unicodedata.normalize("NFD", "엔진")
        paths = resolve_paths({"TEAM_WIKI_HOME": str(tmp_dir / "home")}, tmp_dir / nfd)
        with pytest.raises(ConfigError, match="NFC"):
            build_sync_deps(config_of(), SECRETS, paths, env=ENV, http=idle_client())

    @pytest.mark.parametrize("branch", ["-x", "a..b", "refs/heads/x", "x.lock", "a b"])
    def test_안전하지_않은_데이터_브랜치는_ConfigError(self, tmp_dir: Path, branch: str) -> None:
        with pytest.raises(ConfigError, match="브랜치"):
            build(config_of(dataRepo={"url": "/tmp/data.git", "branch": branch}), tmp_dir)

    def test_안전하지_않은_코드_저장소_브랜치는_ConfigError(self, tmp_dir: Path) -> None:
        repos = [{"name": "worker", "url": "https://github.com/o/w.git", "branch": "-x"}]
        with pytest.raises(ConfigError, match="worker"):
            build(config_of(repos=repos), tmp_dir)

    def test_trust_env가_켜진_클라이언트는_ConfigError(self, tmp_dir: Path) -> None:
        client = httpx2.AsyncClient(trust_env=True, follow_redirects=False)
        with pytest.raises(ConfigError, match="trust_env"):
            build(config_of(), tmp_dir, http=client)

    def test_리다이렉트를_따르는_클라이언트는_ConfigError(self, tmp_dir: Path) -> None:
        client = mock_client(lambda request: httpx2.Response(200), follow_redirects=True)
        with pytest.raises(ConfigError, match="follow_redirects"):
            build(config_of(), tmp_dir, http=client)

    @pytest.mark.parametrize("case", ["nfd", "branch", "url", "token", "http"])
    def test_검증_실패_전에는_부작용이_있는_객체를_만들지_않는다(
        self,
        tmp_dir: Path,
        recorded: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        case: str,
    ) -> None:
        runners: list[object] = []

        def spy(deps: RunnerDeps) -> Callable[..., Any]:
            runners.append(deps)
            raise AssertionError("검증 전에 러너를 만들면 안 된다")

        monkeypatch.setattr(wiring, "create_claude_runner", spy)
        config, paths, secrets, http = config_of(), paths_in(tmp_dir), SECRETS, idle_client()
        if case == "nfd":
            paths = resolve_paths(
                {"TEAM_WIKI_HOME": str(tmp_dir / unicodedata.normalize("NFD", "한"))}, ENGINE_DIR
            )
        elif case == "branch":
            config = config_of(dataRepo={"url": "/tmp/data.git", "branch": "-x"})
        elif case == "url":
            config = config_of(dataRepo={"url": "https://github.com/me/data.git"})
        elif case == "token":
            config = github_config()
            secrets = Secrets(ANTHROPIC_KEY, DISCORD_TOKEN, None)
        else:
            http = httpx2.AsyncClient(trust_env=True)
        with pytest.raises(ConfigError):
            build_sync_deps(config, secrets, paths, env=ENV, http=http)
        assert recorded == []
        assert runners == []
        assert not paths.home.exists()  # 작업 폴더도 만들지 않았다


# ---------------------------------------------------------------- 비밀값(G6)


class TestSecretsStayHidden:
    def test_repr에_비밀값이_없다(self, tmp_dir: Path) -> None:
        for config in (config_of(), github_config()):
            deps = build(config, tmp_dir)
            shown = [
                repr(deps),
                str(deps),
                repr(deps.notifier),
                repr(deps.github),
                repr(deps.data_git),
                repr(deps.code_git),
                repr(deps.pr_port),
                repr(deps.runner),
            ]
            for text in shown:
                for secret in (ANTHROPIC_KEY, DISCORD_TOKEN, GITHUB_TOKEN):
                    assert secret not in text

    def test_redact_값은_repr에_나오지_않지만_들어_있다(self, tmp_dir: Path) -> None:
        deps = build(config_of(), tmp_dir)
        assert "redact" not in repr(deps)
        assert GITHUB_TOKEN in deps.redact

    async def test_알림기는_알려진_비밀값을_본문과_로그에서_지운다(self, tmp_dir: Path) -> None:
        bodies: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            bodies.append(request.content.decode("utf-8"))
            return httpx2.Response(200, json={})

        logs: list[str] = []
        async with mock_client(handler) as http:
            deps = build(config_of(), tmp_dir, http=http, log=logs.append)
            await deps.notifier.send(
                f"실패 {GITHUB_TOKEN} {ANTHROPIC_KEY} {DISCORD_TOKEN}", mention_admin=True
            )
        assert len(bodies) == 1
        content = json.loads(bodies[0])["content"]
        assert f"<@{ADMIN}>" in content
        for secret in (ANTHROPIC_KEY, DISCORD_TOKEN, GITHUB_TOKEN):
            assert secret not in bodies[0]
            assert all(secret not in line for line in logs)
        assert logs  # log 인자가 알림기에 전달됐다
