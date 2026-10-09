"""systemd 유닛 규칙 시험: 하드닝, 경로, 계산한 시간 상한, 종료 코드 정책."""

from __future__ import annotations

import dataclasses
import inspect
import re
import tomllib
from pathlib import Path

import pytest

from team_wiki_engine.claude.runner import RunnerDeps
from team_wiki_engine.config.load import load_config
from team_wiki_engine.config.schema import LimitsConfig
from team_wiki_engine.git import ops as git_ops
from team_wiki_engine.git.client import GitClient
from team_wiki_engine.ops import notify as ops_notify
from team_wiki_engine.sync.ingest import MAX_RAW_PER_RUN
from team_wiki_engine.sync.stage import SyncDeps
from tests.helpers import assumed_deps as A
from tests.helpers.systemd_unit import (
    KNOWN_KEYS,
    UnitFile,
    load_unit,
    parse_bytes,
    parse_timespan,
    parse_unit,
)

ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "deploy" / "systemd"
ENGINE = "/opt/team-wiki-engine"
BIN = ENGINE + "/.venv/bin/"
WORK = "/srv/team-wiki"
UNIT_FILES = (
    "team-wiki-bot.service",
    "team-wiki-notify@.service",
    "team-wiki-pull.service",
    "team-wiki-smoke.service",
    "team-wiki-sync.service",
    "team-wiki-sync.timer",
)
BOT, SYNC, PULL, SMOKE = (
    "team-wiki-bot.service",
    "team-wiki-sync.service",
    "team-wiki-pull.service",
    "team-wiki-smoke.service",
)
NOTIFY = "team-wiki-notify@.service"
TIMER = "team-wiki-sync.timer"
WORKERS = (BOT, SYNC, PULL, SMOKE)
CLAUDE_UNITS = (BOT, SYNC, SMOKE)
SERVICES = (BOT, SYNC, PULL, SMOKE, NOTIFY)
ROLE = {BOT: "bot", SYNC: "sync", PULL: "pull", SMOKE: "smoke"}
HARDENING = {
    "NoNewPrivileges": "yes",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "ProtectKernelLogs": "yes",
    "ProtectControlGroups": "yes",
    "ProtectClock": "yes",
    "ProtectHostname": "yes",
    "RestrictNamespaces": "yes",
    "RestrictRealtime": "yes",
    "RestrictSUIDSGID": "yes",
    "LockPersonality": "yes",
    "SystemCallArchitectures": "native",
    "CapabilityBoundingSet": "",
    "AmbientCapabilities": "",
    "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6 AF_NETLINK",
    "UMask": "0077",
    "KillMode": "control-group",
    "KillSignal": "SIGTERM",
}
FORBIDDEN = (
    "MemoryDenyWriteExecute",
    "SystemCallFilter",
    "StandardOutput",
    "StandardError",
    "DynamicUser",
)


def units() -> dict[str, UnitFile]:
    return {name: load_unit(SYSTEMD / name) for name in UNIT_FILES}


def unknown_keys(unit: UnitFile) -> list[str]:
    bad = []
    for section, keys in unit.sections.items():
        known = KNOWN_KEYS.get(section)
        if known is None:
            bad.append(f"[{section}]")
            continue
        bad.extend(f"{section}.{k}" for k in keys if k not in known)
    return bad


def test_unit_file_list_is_exact() -> None:
    assert sorted(p.name for p in SYSTEMD.iterdir()) == sorted(UNIT_FILES)


def test_all_units_parse_and_use_only_known_keys() -> None:
    for name, unit in units().items():
        assert unknown_keys(unit) == [], name


def test_unknown_key_check_reacts_to_a_typo() -> None:
    bad = parse_unit("[Service]\nProtectSytem=yes\nProtectSystem=strict\n", "typo.service")
    assert unknown_keys(bad) == ["Service.ProtectSytem"]
    assert unknown_keys(parse_unit("[Bogus]\nA=1\n")) == ["[Bogus]"]


def test_all_services_share_the_hardening_block() -> None:
    for name in SERVICES:
        unit = units()[name]
        assert unit.one("Service", "User") == "teamwiki", name
        assert unit.one("Service", "Group") == "teamwiki", name
        assert unit.one("Service", "EnvironmentFile") == "/etc/team-wiki/secrets.env", name
        for key, value in HARDENING.items():
            assert unit.one("Service", key) == value, (name, key)
        assert parse_bytes(unit.one("Service", "MemoryMax")) > 0
        assert int(unit.one("Service", "TasksMax")) > 0
        for key in FORBIDDEN:
            assert not unit.has("Service", key), (name, key)


def test_exec_start_uses_console_scripts_in_the_venv() -> None:
    import shlex

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = set(data["project"]["scripts"]) | A.ASSUMED_SCRIPTS  # ASSUMED: 계획 2·3 스크립트
    for name in SERVICES:
        first = shlex.split(units()[name].one("Service", "ExecStart"))[0]
        assert first.startswith(BIN), name
        base = first.removeprefix(BIN)
        assert base in scripts or (name == SMOKE and base == "python"), (name, base)


def test_worker_environment_and_preflight() -> None:
    base = {
        "HOME": "/srv/team-wiki/home",
        "TEAM_WIKI_HOME": WORK,
        "TEAM_WIKI_CONFIG": "/etc/team-wiki/team.json",
        "TEAM_WIKI_ENGINE_DIR": ENGINE,
        "LANG": "C.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in WORKERS:
        unit = units()[name]
        env = unit.env()
        for k, v in base.items():
            assert env.get(k) == v, (name, k)
        assert ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in env) == (name in CLAUDE_UNITS)
        if name in CLAUDE_UNITS:
            assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert ("LIVE_CLAUDE" in env) == (name == SMOKE)
        import shlex

        pre = shlex.split(unit.one("Service", "ExecStartPre"))
        want = [BIN + "team-wiki-preflight", "--role", ROLE[name]]
        if name != SMOKE:
            want.append("--quiet")
        assert pre == want, name


def _rw_paths(unit: UnitFile) -> list[str]:
    out: list[str] = []
    for value in unit.values("Service", "ReadWritePaths"):
        out.extend(p.lstrip("-") for p in value.split())
    return out


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def test_writable_paths_cover_what_workers_write() -> None:
    for name in WORKERS:
        unit = units()[name]
        env = unit.env()
        rw = _rw_paths(unit)
        needed = [
            env["HOME"],
            env["TEAM_WIKI_HOME"],
            env["TEAM_WIKI_HOME"] + "/state",
            env["TEAM_WIKI_HOME"] + "/data",
            env["TEAM_WIKI_HOME"] + "/data-sync",
            env["TEAM_WIKI_HOME"] + "/repos",
        ]
        for path in needed:
            assert any(_under(path, w) for w in rw), (name, path)
        assert not any(_under(ENGINE, w) for w in rw), name
        assert not any(env["HOME"].startswith(p) for p in ("/home", "/root", "/run/user"))
    assert units()[NOTIFY].values("Service", "ReadWritePaths") == ()


def test_no_secrets_or_proxy_in_environment_and_no_node_leftovers() -> None:
    for name, unit in units().items():
        for key, value in unit.env().items():
            blob = f"{key}={value}"
            assert "proxy" not in blob.lower(), name
            for secret in ("ANTHROPIC_API_KEY", "DISCORD_TOKEN", "GITHUB_TOKEN"):
                assert secret not in blob, name
        text = (SYSTEMD / name).read_text(encoding="utf-8")
        for bad in ("/srv/team-wiki/engine", "node", "dist/", "npm"):
            assert bad not in text, (name, bad)


# --- G3: 상수에서 계산한 시간 상한 ---------------------------------------------------------


def _live_test_info() -> tuple[int, int]:
    text = (ROOT / "tests" / "claude" / "test_live_smoke.py").read_text(encoding="utf-8")
    count = len(re.findall(r"^async def test_", text, flags=re.M))
    match = re.search(r"^TEST_TIMEOUT_S\s*=\s*(\d+)", text, flags=re.M)
    assert match
    return count, int(match.group(1))


def _default(cls: type, name: str) -> float:
    for f in dataclasses.fields(cls):
        if f.name == name:
            assert isinstance(f.default, int | float)
            return float(f.default)
    raise AssertionError(name)


def _timer_interval_s() -> float:
    cal = units()[TIMER].one("Timer", "OnCalendar")
    match = re.fullmatch(r"\*-\*-\* ([0-9,]+):(\d\d):00 \S+", cal)
    assert match
    hours = sorted(int(h) for h in match.group(1).split(","))
    gaps = [b - a for a, b in zip(hours, hours[1:], strict=False)] + [hours[0] + 24 - hours[-1]]
    return min(gaps) * 3600.0


def test_computed_time_limits() -> None:
    us = units()
    git_s = float(inspect.signature(GitClient).parameters["timeout_s"].default)
    net_s = git_ops._NETWORK_TIMEOUT_S
    settle_s = _default(SyncDeps, "settle_timeout_s")
    grace_s = _default(RunnerDeps, "hard_deadline_grace_ms") / 1000
    call_s = LimitsConfig().sync_timeout_ms / 1000
    repos = len(load_config(ROOT / "deploy" / "config" / "team.local.json").repos)
    notify_s = ops_notify._TOTAL_TIMEOUT_SECONDS

    def start(n: str) -> float:
        return parse_timespan(us[n].one("Service", "TimeoutStartSec"))

    def stop(n: str) -> float:
        return parse_timespan(us[n].one("Service", "TimeoutStopSec"))

    assert stop(SYNC) >= net_s + git_s + settle_s + grace_s + 60
    runs = 2 + MAX_RAW_PER_RUN + 2
    assert start(SYNC) >= runs * (call_s + grace_s + settle_s) + (repos + 2) * net_s
    assert start(SYNC) < _timer_interval_s()
    assert start(PULL) >= net_s + 2 * git_s + 60
    assert stop(PULL) >= 2 * git_s + 30
    assert stop(BOT) >= (
        A.REFRESH_STOP_WAIT_S
        + A.SHUTDOWN_GRACE_S
        + A.CANCEL_WAIT_S
        + A.BOT_SETTLE_S
        + notify_s
        + A.GATEWAY_CLOSE_WAIT_S
        + 30
    )
    assert start(BOT) >= A.DATA_LOCK_WAIT_S + A.REFRESH_TIMEOUT_S + 60
    watchdog = parse_timespan(us[BOT].one("Service", "WatchdogSec"))
    assert watchdog >= 60
    count, per_test = _live_test_info()
    assert count > 0
    assert start(SMOKE) >= count * per_test
    for name in SERVICES:
        assert us[name].one("Service", "KillMode") == "control-group"
        assert us[name].one("Service", "KillSignal") == "SIGTERM"


# --- G4: 종료 코드·알림·워치독 -------------------------------------------------------------


def test_exit_code_notify_and_watchdog_policy() -> None:
    us = units()
    bot = us[BOT]
    assert bot.one("Service", "Type") == "notify"
    assert bot.one("Service", "NotifyAccess") == "main"
    assert bot.one("Service", "Restart") == "always"
    assert bot.one("Service", "RestartSec") == "30"
    assert bot.one("Service", "RestartPreventExitStatus") == "2"
    assert bot.one("Unit", "StartLimitIntervalSec") == "600"
    assert bot.one("Unit", "StartLimitBurst") == "10"
    assert bot.one("Install", "WantedBy") == "multi-user.target"
    for name in (BOT, SYNC, PULL, SMOKE):
        assert us[name].values("Unit", "Wants") == ("network-online.target",)
        assert us[name].values("Unit", "After") == ("network-online.target",)
    sync = us[SYNC]
    assert sync.one("Service", "Type") == "oneshot"
    assert sync.one("Service", "SuccessExitStatus") == "1"
    assert "Install" not in sync.sections
    pull = us[PULL]
    assert pull.one("Service", "Type") == "oneshot"
    assert not pull.has("Service", "SuccessExitStatus")
    assert "Install" not in pull.sections
    for name in (BOT, SYNC, PULL):
        assert us[name].values("Unit", "OnFailure") == ("team-wiki-notify@%n.service",)
    for name in (NOTIFY, SMOKE):
        assert not us[name].has("Unit", "OnFailure")
        assert "Install" not in us[name].sections
    assert us[NOTIFY].one("Service", "ExecStart").endswith(" %i")


def test_smoke_unit_runs_the_live_file() -> None:
    import shlex

    argv = shlex.split(units()[SMOKE].one("Service", "ExecStart"))
    assert "-m" in argv and "pytest" in argv
    assert argv[argv.index("-m", argv.index("pytest")) + 1] == "live"
    assert "no:cacheprovider" in argv
    assert argv[-1] == ENGINE + "/tests/claude/test_live_smoke.py"
    assert (ROOT / "tests" / "claude" / "test_live_smoke.py").is_file()


def test_timer_matches_config_and_is_the_only_timer() -> None:
    us = units()
    timer = us[TIMER]
    match = re.fullmatch(
        r"\*-\*-\* (\d\d(?:,\d\d)*):(\d\d):00 (\S+)", timer.one("Timer", "OnCalendar")
    )
    assert match
    cfg = load_config(ROOT / "deploy" / "config" / "team.local.json")
    times = [f"{h}:{match.group(2)}" for h in match.group(1).split(",")]
    assert times == list(cfg.schedule)
    assert match.group(3) == cfg.timezone
    assert timer.one("Timer", "Persistent") == "yes"
    assert timer.one("Timer", "RandomizedDelaySec") == "0"
    target = timer.one("Timer", "Unit")
    assert (SYSTEMD / target).is_file()
    assert timer.one("Install", "WantedBy") == "timers.target"
    timers = [n for n in UNIT_FILES if n.endswith(".timer")]
    assert timers == [TIMER]
    assert target == SYNC


def test_memory_budget() -> None:
    names = (BOT, SYNC, PULL, NOTIFY)
    total = sum(parse_bytes(units()[n].one("Service", "MemoryMax")) for n in names)
    assert total <= 8 * 2**30


def test_assumptions_are_recorded() -> None:
    """병합 뒤 비어야 하는 가정 목록(보고서용). 여기서는 형식만 확인한다."""
    assert all(isinstance(x, str) for x in A.ASSUMED)


@pytest.mark.parametrize("name", UNIT_FILES)
def test_unit_files_are_lf_only_and_have_header_comment(name: str) -> None:
    raw = (SYSTEMD / name).read_bytes()
    assert b"\r" not in raw
    assert raw.startswith(b"#")
