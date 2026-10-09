import dataclasses
from pathlib import Path

import pytest

from team_wiki_engine.config.load import (
    ConfigError,
    Paths,
    Secrets,
    load_config,
    load_secrets,
    parse_config,
    resolve_paths,
)
from team_wiki_engine.config.schema import (
    DataRepoConfig,
    DiscordConfig,
    EffortConfig,
    LimitsConfig,
    RepoConfig,
    TeamConfig,
)
from tests.helpers import write_tree

SERVER_REPO = {"name": "server", "url": "https://github.com/o/server.git"}
MINIMAL: dict[str, object] = {
    "repos": [SERVER_REPO],
    "dataRepo": {"url": "/tmp/data.git"},
}

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "team.example.json"


class TestParseConfig:
    def test_최소_설정에_기본값을_채운다(self) -> None:
        c = parse_config(MINIMAL)
        assert c.delivery == "local"
        assert c.review_mode == "pr"
        assert c.model == "claude-sonnet-5-5"
        assert c.effort == EffortConfig(bot="low", sync="medium")
        assert c.schedule == ["09:00", "21:00"]
        assert c.timezone == "Asia/Seoul"
        assert c.limits == LimitsConfig(
            per_user_per_minute=2,
            daily_total=60,
            concurrent=2,
            bot_timeout_ms=300000,
            sync_timeout_ms=1200000,
            diff_bytes_per_repo=200000,
            pr_body_chars=4000,
        )
        assert c.discord == DiscordConfig(
            guild_id="", channel_ids=[], ops_channel_id="", admin_user_id=""
        )
        assert c.repos[0] == RepoConfig(
            name="server",
            url="https://github.com/o/server.git",
            branch="main",
            status="active",
        )
        assert c.data_repo == DataRepoConfig(url="/tmp/data.git", branch="main")

    def test_기본값_객체는_인스턴스마다_독립이다(self) -> None:
        a = parse_config(MINIMAL)
        b = parse_config(MINIMAL)
        a.schedule.append("12:00")
        a.discord.channel_ids.append("1")
        assert b.schedule == ["09:00", "21:00"]
        assert b.discord.channel_ids == []

    def test_camel_case_키를_받아_snake_case_속성으로_노출한다(self) -> None:
        c = parse_config(
            {
                **MINIMAL,
                "reviewMode": "direct",
                "limits": {"botTimeoutMs": 1000, "perUserPerMinute": 5},
                "discord": {"guildId": "g", "channelIds": ["1", "2"], "opsChannelId": "o"},
            }
        )
        assert c.review_mode == "direct"
        assert c.data_repo.url == "/tmp/data.git"
        assert c.limits.bot_timeout_ms == 1000
        assert c.limits.per_user_per_minute == 5
        assert c.discord.channel_ids == ["1", "2"]
        assert c.discord.ops_channel_id == "o"

    def test_코드에서는_snake_case_이름으로도_만들_수_있다(self) -> None:
        c = TeamConfig(
            repos=[RepoConfig(name="a", url="x")],
            data_repo=DataRepoConfig(url="y"),
            review_mode="direct",
        )
        assert c.review_mode == "direct"
        assert c.limits == LimitsConfig()

    def test_저장소_이름_중복을_키_경로와_함께_알린다(self) -> None:
        raw = {**MINIMAL, "repos": [SERVER_REPO, {"name": "server", "url": "x"}]}
        with pytest.raises(ConfigError, match=r"repos\.1\.name: 저장소 이름 중복: server"):
            parse_config(raw)

    def test_중복이_여러_번이면_줄마다_알린다(self) -> None:
        repos = [{"name": n, "url": "x"} for n in ("a", "b", "a", "b")]
        with pytest.raises(ConfigError) as info:
            parse_config({**MINIMAL, "repos": repos})
        lines = str(info.value).splitlines()
        assert "- repos.2.name: 저장소 이름 중복: a" in lines
        assert "- repos.3.name: 저장소 이름 중복: b" in lines

    def test_이름_형식을_검사한다(self) -> None:
        with pytest.raises(ConfigError) as info:
            parse_config({**MINIMAL, "repos": [{"name": "Server", "url": "x"}]})
        assert "- repos.0.name: 소문자, 숫자, 하이픈만 쓸 수 있습니다" in str(info.value)

    def test_이름은_줄바꿈이_끼어도_통과하지_못한다(self) -> None:
        with pytest.raises(ConfigError, match=r"repos\.0\.name"):
            parse_config({**MINIMAL, "repos": [{"name": "server\n", "url": "x"}]})

    def test_시각_형식을_검사한다(self) -> None:
        with pytest.raises(ConfigError) as info:
            parse_config({**MINIMAL, "schedule": ["9:00"]})
        assert "- schedule.0: HH:MM 형식이어야 합니다" in str(info.value)
        for bad in ("24:00", "12:60", "٠٩:٠٠", "09:00\n"):
            with pytest.raises(ConfigError, match=r"schedule\.0"):
                parse_config({**MINIMAL, "schedule": [bad]})
        assert parse_config({**MINIMAL, "schedule": ["00:00", "23:59"]}).schedule == [
            "00:00",
            "23:59",
        ]

    def test_effort_값을_검사한다(self) -> None:
        with pytest.raises(ConfigError, match=r"- effort\.bot: 허용된 값이 아닙니다"):
            parse_config({**MINIMAL, "effort": {"bot": "huge"}})
        for value in ("low", "medium", "high", "xhigh", "max"):
            assert parse_config({**MINIMAL, "effort": {"sync": value}}).effort.sync == value

    def test_limits는_양의_정수여야_한다(self) -> None:
        with pytest.raises(ConfigError) as info:
            parse_config({**MINIMAL, "limits": {"dailyTotal": 0}})
        assert "- limits.dailyTotal: 1 이상의 정수여야 합니다" in str(info.value)
        for bad in (-1, 1.5, "3", True):
            with pytest.raises(ConfigError, match=r"limits\.dailyTotal"):
                parse_config({**MINIMAL, "limits": {"dailyTotal": bad}})

    def test_저장소는_최소_한_개가_필요하다(self) -> None:
        with pytest.raises(ConfigError, match=r"- repos: 최소 1개 이상 필요합니다"):
            parse_config({**MINIMAL, "repos": []})

    def test_필수_키_누락을_알린다(self) -> None:
        with pytest.raises(ConfigError) as info:
            parse_config({})
        lines = str(info.value).splitlines()
        assert "- repos: 필수 항목입니다" in lines
        assert "- dataRepo: 필수 항목입니다" in lines

    def test_알_수_없는_키를_거부한다(self) -> None:
        with pytest.raises(ConfigError, match=r"- modle: 알 수 없는 키입니다"):
            parse_config({**MINIMAL, "modle": "x"})
        with pytest.raises(ConfigError, match=r"- limits\.dailyTotl: 알 수 없는 키입니다"):
            parse_config({**MINIMAL, "limits": {"dailyTotl": 3}})
        with pytest.raises(ConfigError, match=r"- repos\.0\.brnach: 알 수 없는 키입니다"):
            parse_config({**MINIMAL, "repos": [{"name": "a", "url": "x", "brnach": "m"}]})

    def test_빈_문자열과_잘못된_타입을_거부한다(self) -> None:
        with pytest.raises(ConfigError, match=r"- model: 비어 있을 수 없습니다"):
            parse_config({**MINIMAL, "model": ""})
        with pytest.raises(ConfigError, match=r"- delivery: 허용된 값이 아닙니다"):
            parse_config({**MINIMAL, "delivery": "email"})
        with pytest.raises(ConfigError, match=r"- timezone: 문자열이어야 합니다"):
            parse_config({**MINIMAL, "timezone": 9})
        with pytest.raises(ConfigError, match=r"- discord: 객체여야 합니다"):
            parse_config({**MINIMAL, "discord": []})

    def test_최상위가_객체가_아니면_root로_알린다(self) -> None:
        with pytest.raises(ConfigError, match=r"- \(root\): 객체여야 합니다"):
            parse_config([])

    def test_검증_실패는_ConfigError다(self) -> None:
        with pytest.raises(ConfigError):
            parse_config({})

    def test_오류_메시지에_입력값이_나오지_않는다(self) -> None:
        with pytest.raises(ConfigError) as info:
            parse_config({**MINIMAL, "model": 123456789, "zzz": "sk-ant-should-not-appear"})
        message = str(info.value)
        assert "sk-ant-should-not-appear" not in message
        assert "123456789" not in message


class TestLoadConfig:
    def test_저장소의_예시_설정이_검증을_통과한다(self) -> None:
        c = load_config(EXAMPLE_CONFIG)
        assert [r.name for r in c.repos] == ["server", "relay", "worker", "web"]
        assert c.delivery == "local"

    def test_문자열_경로도_받는다(self) -> None:
        assert load_config(str(EXAMPLE_CONFIG)).delivery == "local"

    def test_없는_파일과_JSON_오류를_구분해_알린다(self, tmp_dir: Path) -> None:
        missing = tmp_dir / "nonexistent" / "team.json"
        with pytest.raises(ConfigError, match=r"설정 파일을 읽을 수 없습니다: ") as info:
            load_config(missing)
        assert str(missing) in str(info.value)

        write_tree(tmp_dir, {"team.json": "{ not json"})
        file = tmp_dir / "team.json"
        with pytest.raises(ConfigError, match=r"설정 파일이 JSON이 아닙니다: .* \(.+\)") as info:
            load_config(file)
        assert str(file) in str(info.value)

    def test_폴더를_넘기면_읽을_수_없다고_알린다(self, tmp_dir: Path) -> None:
        with pytest.raises(ConfigError, match=r"읽을 수 없습니다"):
            load_config(tmp_dir)

    def test_UTF8이_아닌_파일은_읽을_수_없다고_알린다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "team.json"
        file.write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(ConfigError, match=r"읽을 수 없습니다"):
            load_config(file)

    def test_너무_긴_정수_리터럴은_JSON이_아니라고_알린다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "team.json"
        file.write_text("1" * 5000, encoding="utf-8")
        with pytest.raises(ConfigError, match=r"설정 파일이 JSON이 아닙니다: .* \(.+\)") as info:
            load_config(file)
        assert "1" * 50 not in str(info.value)

    def test_NUL_문자가_든_경로도_ConfigError로_알린다(self) -> None:
        with pytest.raises(ConfigError, match=r"설정 파일을 읽을 수 없습니다: a\\x00b") as info:
            load_config("a\x00b")
        assert "\x00" not in str(info.value)

    def test_검증_실패는_파일에서도_ConfigError다(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"team.json": '{"modle": "x"}'})
        with pytest.raises(ConfigError, match=r"- modle: 알 수 없는 키입니다"):
            load_config(tmp_dir / "team.json")


class TestLoadSecrets:
    config = parse_config(MINIMAL)
    github_config = parse_config({**MINIMAL, "delivery": "github"})

    def test_봇은_디스코드_토큰이_필요하다(self) -> None:
        with pytest.raises(ConfigError, match=r"DISCORD_TOKEN"):
            load_secrets({"ANTHROPIC_API_KEY": "k"}, self.config, "bot")

    def test_동기화는_local_모드에서_GitHub_토큰_없이_돈다(self) -> None:
        secrets = load_secrets({"ANTHROPIC_API_KEY": "k"}, self.config, "sync")
        assert secrets.anthropic_api_key == "k"
        assert secrets.discord_token is None
        assert secrets.github_token is None

    def test_github_전달_모드는_GitHub_토큰이_필요하다(self) -> None:
        with pytest.raises(ConfigError, match=r"GITHUB_TOKEN \(delivery: github\)"):
            load_secrets({"ANTHROPIC_API_KEY": "k"}, self.github_config, "sync")

    def test_Anthropic_키는_언제나_필요하고_공백만_있으면_없는_것으로_본다(self) -> None:
        with pytest.raises(ConfigError, match=r"ANTHROPIC_API_KEY"):
            load_secrets({"ANTHROPIC_API_KEY": "   "}, self.config, "sync")
        with pytest.raises(ConfigError, match=r"ANTHROPIC_API_KEY"):
            load_secrets({}, self.config, "sync")

    def test_값은_앞뒤_공백을_지운다(self) -> None:
        secrets = load_secrets(
            {"ANTHROPIC_API_KEY": " k\n", "DISCORD_TOKEN": "\td ", "GITHUB_TOKEN": " g "},
            self.github_config,
            "bot",
        )
        assert secrets == dataclasses.replace(
            secrets, anthropic_api_key="k", discord_token="d", github_token="g"
        )

    def test_빠진_변수를_모두_한_번에_알린다(self) -> None:
        with pytest.raises(ConfigError) as info:
            load_secrets({}, self.github_config, "bot")
        message = str(info.value)
        for name in ("ANTHROPIC_API_KEY", "DISCORD_TOKEN", "GITHUB_TOKEN"):
            assert name in message

    def test_에러_메시지에_값이_아닌_변수_이름만_나온다(self) -> None:
        with pytest.raises(ConfigError) as info:
            load_secrets({"ANTHROPIC_API_KEY": "sk-ant-should-not-appear"}, self.config, "bot")
        assert "sk-ant-should-not-appear" not in str(info.value)
        assert "DISCORD_TOKEN" in str(info.value)

    def test_repr과_str에_비밀값이_나오지_않는다(self) -> None:
        values = ("sk-ant-SECRET-1", "discord-SECRET-2", "ghp_SECRET_3")
        secrets = load_secrets(
            {"ANTHROPIC_API_KEY": values[0], "DISCORD_TOKEN": values[1], "GITHUB_TOKEN": values[2]},
            self.github_config,
            "bot",
        )
        for text in (repr(secrets), str(secrets), f"{secrets}", f"{secrets!r}"):
            for value in values:
                assert value not in text
        assert secrets == Secrets(*values)
        assert secrets != Secrets(values[0], values[1], None)

    def test_Secrets는_변경할_수_없다(self) -> None:
        secrets = load_secrets({"ANTHROPIC_API_KEY": "k"}, self.config, "sync")
        with pytest.raises(dataclasses.FrozenInstanceError):
            secrets.anthropic_api_key = "other"  # type: ignore[misc]


class TestResolvePaths:
    def test_기본_홈은_srv_team_wiki다(self) -> None:
        p = resolve_paths({}, "/opt/engine")
        assert p.home == Path("/srv/team-wiki")
        assert p.engine_dir == Path("/opt/engine")
        assert p.data_dir == Path("/srv/team-wiki/data")
        assert p.data_sync_dir == Path("/srv/team-wiki/data-sync")
        assert p.repos_dir == Path("/srv/team-wiki/repos")
        assert p.state_dir == Path("/srv/team-wiki/state")
        assert p.runs_dir == Path("/srv/team-wiki/state/runs")
        assert p.local_prs_dir == Path("/srv/team-wiki/state/local-prs")
        assert p.skills_dir == Path("/opt/engine/skills")

    def test_TEAM_WIKI_HOME으로_바꿀_수_있다(self) -> None:
        p = resolve_paths({"TEAM_WIKI_HOME": "/tmp/twh"}, "/opt/engine")
        assert p.data_dir == Path("/tmp/twh/data")
        assert p.runs_dir == Path("/tmp/twh/state/runs")

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_TEAM_WIKI_HOME이_비었거나_공백뿐이면_기본값이다(self, blank: str) -> None:
        assert resolve_paths({"TEAM_WIKI_HOME": blank}, "/opt/engine").home == Path(
            "/srv/team-wiki"
        )

    def test_TEAM_WIKI_HOME의_앞뒤_공백은_벗겨서_쓴다(self) -> None:
        p = resolve_paths({"TEAM_WIKI_HOME": "  /tmp/twh \n"}, "/opt/engine")
        assert p.home == Path("/tmp/twh")
        assert p.data_dir == Path("/tmp/twh/data")

    def test_공백이_앞에_붙은_상대_경로도_벗긴_값_기준으로_절대화한다(self) -> None:
        p = resolve_paths({"TEAM_WIKI_HOME": "  rel/home "}, "/opt/engine")
        assert p.home == Path.cwd() / "rel" / "home"

    def test_모든_필드는_절대_경로다(self) -> None:
        p = resolve_paths({"TEAM_WIKI_HOME": "relative/home"}, "relative/engine")
        for field in dataclasses.fields(Paths):
            value = getattr(p, field.name)
            assert isinstance(value, Path), field.name
            assert value.is_absolute(), field.name
        assert p.home == Path.cwd() / "relative" / "home"
        assert p.skills_dir == Path.cwd() / "relative" / "engine" / "skills"

    def test_문자열과_Path_엔진_폴더를_모두_받는다(self) -> None:
        assert resolve_paths({}, Path("/opt/engine")) == resolve_paths({}, "/opt/engine")

    def test_경로를_정규화한다(self) -> None:
        p = resolve_paths({"TEAM_WIKI_HOME": "/tmp/a/../twh/"}, "/opt/x/./engine")
        assert p.home == Path("/tmp/twh")
        assert p.engine_dir == Path("/opt/x/engine")
