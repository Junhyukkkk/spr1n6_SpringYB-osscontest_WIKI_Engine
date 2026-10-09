"""봇 조립: 설정 검증, `data` 사본용 git, 오늘 사용량 시드, 의존성."""

import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.bot import wiring
from team_wiki_engine.bot.wiring import bot_settings, build_bot_deps, data_git_for, today_usage
from team_wiki_engine.claude.runner import RunnerDeps
from team_wiki_engine.config.load import ConfigError, Paths, Secrets, parse_config, resolve_paths
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.git.client import GitClient
from team_wiki_engine.metrics.record import STATE_FILES, QuestionRecord, append_jsonl
from tests.helpers.bot import FakeGateway
from tests.helpers.http import idle_client
from tests.helpers.runner import ScriptedRunner

ANTHROPIC_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
# 가짜 값. 한 문자열로 쓰면 GitHub push 보호가 진짜 디스코드 토큰으로 보고 push를 막는다.
DISCORD_TOKEN = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4", "GhIjKl", "AbCdEfGhIjKlMnOpQrStUvWxYz012345"])
GITHUB_TOKEN = "ghp_Zx81QmN4vT7bK2pLs9Wd3Yc6Hf0JrAeU5"
SECRETS = Secrets(ANTHROPIC_KEY, DISCORD_TOKEN, GITHUB_TOKEN)
NOW = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)


def config_of(**overrides: Any) -> TeamConfig:
    discord = {
        "guildId": "100",
        "channelIds": ["200"],
        "opsChannelId": "300",
        "adminUserId": "400",
    }
    raw: dict[str, Any] = {
        "repos": [{"name": "worker", "url": "https://github.com/o/worker.git"}],
        "dataRepo": {"url": "/tmp/data.git"},
        "discord": discord,
        **overrides,
    }
    return parse_config(raw)


def with_discord(**fields: Any) -> TeamConfig:
    base = {"guildId": "100", "channelIds": ["200"], "opsChannelId": "300", "adminUserId": "400"}
    return config_of(discord={**base, **fields})


class TestBotSettings:
    def test_ok_dedupes_channels(self) -> None:
        s = bot_settings(with_discord(channelIds=["200", "200", "201"]))
        assert s.guild_id == "100" and s.channel_ids == frozenset({"200", "201"})
        assert s.admin_user_id == "400" and s.ops_channel_id == "300"

    @pytest.mark.parametrize("guild", ["", "g1", "12\n"])
    def test_bad_guild(self, guild: str) -> None:
        with pytest.raises(ConfigError):
            bot_settings(with_discord(guildId=guild))

    @pytest.mark.parametrize("channels", [[], ["c1"], ["200", "x"]])
    def test_bad_channels(self, channels: list[str]) -> None:
        with pytest.raises(ConfigError):
            bot_settings(with_discord(channelIds=channels))

    def test_optional_ids(self) -> None:
        s = bot_settings(with_discord(adminUserId="", opsChannelId=""))
        assert s.admin_user_id is None and s.ops_channel_id is None
        with pytest.raises(ConfigError) as info:
            bot_settings(with_discord(adminUserId="abc"))
        assert "abc" not in str(info.value)
        with pytest.raises(ConfigError):
            bot_settings(with_discord(opsChannelId="abc"))


class Recorder(GitClient):
    made: list[dict[str, Any]] = []

    def __init__(self, cwd: Path, **kwargs: Any) -> None:
        Recorder.made.append({"cwd": cwd, **kwargs})
        super().__init__(cwd, **kwargs)


class TestDataGit:
    def paths(self, tmp_dir: Path) -> Paths:
        return resolve_paths({"TEAM_WIKI_HOME": str(tmp_dir)}, tmp_dir / "engine")

    def test_local_rejects_url(self, tmp_dir: Path) -> None:
        cfg = config_of(dataRepo={"url": "https://github.com/me/data.git"})
        with pytest.raises(ConfigError):
            data_git_for(cfg, SECRETS, self.paths(tmp_dir), {})

    def test_local_has_no_auth_and_file_only(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        Recorder.made = []
        monkeypatch.setattr(wiring, "GitClient", Recorder)
        git = data_git_for(config_of(), SECRETS, self.paths(tmp_dir), {"PATH": "/usr/bin"})
        assert git.has_auth is False
        assert Recorder.made[0]["allowed_protocols"] == "file"
        assert Recorder.made[0]["cwd"] == self.paths(tmp_dir).data_dir

    def test_github_has_auth_and_https(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        Recorder.made = []
        monkeypatch.setattr(wiring, "GitClient", Recorder)
        cfg = config_of(delivery="github", dataRepo={"url": "https://github.com/me/data.git"})
        git = data_git_for(cfg, SECRETS, self.paths(tmp_dir), {})
        assert git.has_auth is True
        assert Recorder.made[0]["allowed_protocols"] == "https"

    def test_github_bad_url_and_bad_branch(self, tmp_dir: Path) -> None:
        cfg = config_of(delivery="github", dataRepo={"url": "/tmp/x.git"})
        with pytest.raises(ConfigError):
            data_git_for(cfg, SECRETS, self.paths(tmp_dir), {})
        bad = config_of(dataRepo={"url": "/tmp/data.git", "branch": "-x"})
        with pytest.raises(ConfigError):
            data_git_for(bad, SECRETS, self.paths(tmp_dir), {})


def put(state: Path, at: str, cost: float = 0.01) -> None:
    append_jsonl(
        state / STATE_FILES.questions,
        QuestionRecord(
            at=at,
            user="u",
            thread_id="9",
            ok=True,
            failure=None,
            source="wiki",
            duration_ms=1,
            cost_usd=cost,
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            resumed=False,
        ),
    )


class TestTodayUsage:
    def test_counts_only_today_kst(self, tmp_dir: Path) -> None:
        put(tmp_dir, "2026-10-06T23:59:59+09:00")
        put(tmp_dir, "2026-10-06T14:59:59Z")  # KST 23:59:59 어제
        put(tmp_dir, "2026-10-06T15:00:00Z")  # KST 00:00 오늘
        put(tmp_dir, "2026-10-07T09:00:00+09:00")
        put(tmp_dir, "2026-10-07T23:59:59+09:00")
        count, cost = today_usage(tmp_dir, NOW)
        assert count == 3 and cost == pytest.approx(0.03)

    def test_missing_file(self, tmp_dir: Path) -> None:
        assert today_usage(tmp_dir, NOW) == (0, 0.0)


class TestBuildBotDeps:
    def paths(self, home: Path) -> Paths:
        return resolve_paths({"TEAM_WIKI_HOME": str(home)}, home / "engine")

    def build(self, home: Path, **kwargs: Any) -> Any:
        return build_bot_deps(
            config_of(),
            SECRETS,
            self.paths(home),
            env=kwargs.pop("env", {"ANTHROPIC_API_KEY": ANTHROPIC_KEY}),
            http=idle_client(),
            gateway=FakeGateway(),
            now=lambda: NOW,
            **kwargs,
        )

    def test_repr_hides_secrets(self, tmp_dir: Path) -> None:
        deps = self.build(tmp_dir, runner=ScriptedRunner())
        shown = repr(deps) + repr(deps.pipeline) + repr(deps.dispatcher)
        for secret in (ANTHROPIC_KEY, DISCORD_TOKEN, GITHUB_TOKEN):
            assert secret not in shown

    def test_nfd_home_rejected(self, tmp_dir: Path) -> None:
        home = tmp_dir / unicodedata.normalize("NFD", "한글")
        with pytest.raises(ConfigError):
            self.build(home, runner=ScriptedRunner())

    def test_seeds_daily_usage(self, tmp_dir: Path) -> None:
        paths = self.paths(tmp_dir)
        for _ in range(59):
            put(paths.state_dir, "2026-10-07T09:00:00+09:00")
        deps = self.build(tmp_dir, runner=ScriptedRunner())
        assert deps.pipeline._d.limiter.daily_used() == 59
        assert deps.pipeline.cost_today() == pytest.approx(0.59)

    def test_given_runner_is_used(self, tmp_dir: Path) -> None:
        runner = ScriptedRunner()
        deps = self.build(tmp_dir, runner=runner)
        assert deps.pipeline._d.runner is runner

    def test_default_runner_uses_runner_deps_from_config(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[RunnerDeps] = []
        real = wiring.runner_deps_from_config

        def spy(config: TeamConfig, paths: Paths, env: Any) -> RunnerDeps:
            deps = real(config, paths, env)
            seen.append(deps)
            return deps

        monkeypatch.setattr(wiring, "runner_deps_from_config", spy)
        cfg = config_of()
        build_bot_deps(
            cfg,
            SECRETS,
            self.paths(tmp_dir),
            env={"ANTHROPIC_API_KEY": ANTHROPIC_KEY},
            http=idle_client(),
            gateway=FakeGateway(),
            now=lambda: NOW,
        )
        assert len(seen) == 1 and seen[0].effort is cfg.effort


class TestUserSaltWiring:
    def test_salt_is_private_not_guild_id(self, tmp_dir: Path) -> None:
        t = TestBuildBotDeps()
        deps = t.build(tmp_dir, runner=ScriptedRunner())
        salt = (t.paths(tmp_dir).state_dir / "user-salt").read_text()
        assert len(salt) == 64
        assert deps.dispatcher._salt == salt
        assert deps.pipeline._d.user_salt == salt
        assert salt != config_of().discord.guild_id
