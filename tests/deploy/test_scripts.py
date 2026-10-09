"""설치·업데이트·되돌리기 스크립트 시험(DRY_RUN과 임시 폴더만, 서버 명령 실행 없음)."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers.deploy_scripts import (
    bash_candidates,
    deploy_dirs,
    run_deploy_script,
    write_fake_systemctl,
)

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SETUP, UPDATE, LIB = DEPLOY / "setup.sh", DEPLOY / "update.sh", DEPLOY / "lib.sh"
STANDALONE = DEPLOY / "run-standalone.sh"
SCRIPTS = (LIB, SETUP, UPDATE, STANDALONE)
BASHES = bash_candidates()
FORBIDDEN = (
    "declare -A", "mapfile", "readarray", ",,}", "^^}", "|&", "&>>", "coproc",
    "curl", "| bash", "npm", "node",
)  # fmt: skip
UNIT_LIST = sorted(os.listdir(DEPLOY / "systemd"))


def norm(lines: list[str]) -> list[str]:
    return [" ".join(ln[2:].split()) for ln in lines]


def index(lines: list[str], needle: str, start: int = 0) -> int:
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i
    raise AssertionError(f"{needle!r} 없음 (start={start}): {lines}")


def has(lines: list[str], needle: str) -> bool:
    return any(needle in ln for ln in lines)


def test_syntax_and_portability() -> None:
    for bash in BASHES:
        for script in SCRIPTS:
            done = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
            assert done.returncode == 0, (bash, script, done.stderr)
    for script in SCRIPTS:
        text = script.read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, (script.name, bad)
    assert shutil.which("bash")


def test_shellcheck_when_available() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck 없음")
    done = subprocess.run(
        ["shellcheck", "-x", *map(str, SCRIPTS)], capture_output=True, text=True, cwd=ROOT
    )
    assert done.returncode == 0, done.stdout


def test_units_list_matches_directory() -> None:
    for line in LIB.read_text(encoding="utf-8").splitlines():
        if line.startswith("UNITS="):
            words = line.split("=", 1)[1].strip('"').split()
            assert sorted(words) == UNIT_LIST
            return
    raise AssertionError("UNITS 없음")


@pytest.mark.parametrize("bash", BASHES)
class TestSetup:
    def env(self, tmp_path: Path, **extra: str) -> dict[str, str]:
        return {
            **deploy_dirs(tmp_path),
            "APP_USER": "team-wiki-test-nouser-zz",
            "ENGINE_REPO": "/tmp/x.bundle",
            **extra,
        }

    def test_bad_inputs(self, bash: str, tmp_path: Path) -> None:
        e = self.env(tmp_path)
        del e["ENGINE_REPO"]
        r = run_deploy_script(SETUP, e, bash=bash)
        assert r.code == 2 and "ENGINE_REPO" in r.err
        assert run_deploy_script(SETUP, self.env(tmp_path, ENGINE_REPO="-x"), bash=bash).code == 2
        for ref in ("-x", "a..b", "a b"):
            r = run_deploy_script(SETUP, self.env(tmp_path, ENGINE_REF=ref), bash=bash)
            assert r.code == 2, ref

    def test_first_install_order_and_no_start(self, bash: str, tmp_path: Path) -> None:
        env = self.env(tmp_path)
        r = run_deploy_script(SETUP, env, bash=bash)
        assert r.code == 0, r.err
        lines = norm(r.lines)
        eng, etc, home = env["ENGINE_DIR"], env["ETC_DIR"], env["APP_HOME"]
        steps = [
            "apt-get install -y --no-install-recommends git python3.12 python3.12-venv "
            "tzdata ca-certificates",
            f"useradd --system --user-group --home-dir {home}/home --no-create-home "
            "--shell /usr/sbin/nologin",
            f"git clone --quiet --branch main -- /tmp/x.bundle {eng}",
            f"python3.12 -m venv {eng}/.venv",
            f"-e {eng}[dev]",
            f"chown -R root:root {eng}",
            f"install -o root -g team-wiki-test-nouser-zz -m 0640 "
            f"{eng}/deploy/config/team.local.json {etc}/team.json",
            f"install -o root -g root -m 0600 {eng}/deploy/secrets.env.example "
            f"{etc}/secrets.env",
        ]
        at = 0
        for step in steps:
            at = index(lines, step, at) + 1
        for unit in UNIT_LIST:
            assert has(lines, f"-m 0644 {eng}/deploy/systemd/{unit} {env['SYSTEMD_DIR']}/{unit}")
        at = index(lines, "systemd-tmpfiles --create", at)
        at = index(lines, "systemctl daemon-reload", at)
        index(lines, "systemctl enable team-wiki-bot.service team-wiki-sync.timer", at)
        for ln in lines:
            assert "systemctl start" not in ln and "--now" not in ln
            assert "restart team-wiki" not in ln

    def test_rerun_keeps_existing_state(self, bash: str, tmp_path: Path) -> None:
        env = self.env(tmp_path, APP_USER=getpass.getuser())
        eng, etc = Path(env["ENGINE_DIR"]), Path(env["ETC_DIR"])
        (eng / ".git").mkdir(parents=True)
        py = eng / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)
        etc.mkdir(parents=True)
        (etc / "team.json").write_text("{}")
        (etc / "secrets.env").write_text("ANTHROPIC_API_KEY=filled\n")
        r = run_deploy_script(SETUP, env, bash=bash)
        assert r.code == 0, r.err
        lines = norm(r.lines)
        for needle in ("useradd", "git clone", "-m venv", f"{etc}/team.json", f"{etc}/secrets.env"):
            assert not has(lines, needle), needle
        assert has(lines, "-e " + str(eng) + "[dev]")
        assert has(lines, "-m 0644")
        assert (etc / "secrets.env").read_text() == "ANTHROPIC_API_KEY=filled\n"


SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize("bash", BASHES)
class TestUpdate:
    def env(self, tmp_path: Path, **extra: str) -> dict[str, str]:
        env = deploy_dirs(tmp_path)
        eng = Path(env["ENGINE_DIR"])
        eng.mkdir(parents=True, exist_ok=True)
        if not (eng / ".git").exists():
            git = ["git", "-C", str(eng)]
            subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
            subprocess.run(
                [*git, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "x"],
                check=True,
            )  # fmt: skip
        return {
            **env,
            "SYSTEMCTL": str(write_fake_systemctl(tmp_path)),
            "FAKE_STATE_team_wiki_sync_timer": "active",
            **extra,
        }

    def head(self, env: dict[str, str]) -> str:
        done = subprocess.run(
            ["git", "-C", env["ENGINE_DIR"], "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        return done.stdout.strip()

    def test_bad_args(self, bash: str, tmp_path: Path) -> None:
        env = self.env(tmp_path)
        assert run_deploy_script(UPDATE, env, args=["--x"], bash=bash).code == 2
        assert run_deploy_script(UPDATE, env, args=["a b"], bash=bash).code == 2
        r = run_deploy_script(UPDATE, {**env, "ENGINE_REMOTE": "--upload-pack=x"}, bash=bash)
        assert r.code == 2

    def test_normal_update_order(self, bash: str, tmp_path: Path) -> None:
        env = self.env(tmp_path)
        r = run_deploy_script(UPDATE, env, bash=bash)
        assert r.code == 0, r.err
        lines = norm(r.lines)
        eng = env["ENGINE_DIR"]
        steps = [
            "systemctl stop team-wiki-sync.timer",
            f"team-wiki-backup --dest {env['BACKUP_DIR']}",
            f"직전 커밋 기록: {self.head(env)}",
            f"git -C {eng} fetch --quiet -- origin main",
            f"git -C {eng} merge --ff-only --quiet FETCH_HEAD",
            "-e " + eng + "[dev]",
            "systemctl daemon-reload",
            "systemctl try-restart team-wiki-bot.service",
            "systemctl start team-wiki-sync.timer",
        ]
        at = 0
        for step in steps:
            at = index(lines, step, at) + 1
        assert at == len(lines)
        for unit in UNIT_LIST:
            assert has(lines, f"{eng}/deploy/systemd/{unit}")

    @pytest.mark.parametrize(
        "unit", ["team_wiki_sync_service", "team_wiki_pull_service"]
    )
    @pytest.mark.parametrize("state", ["activating", "active"])
    def test_refuses_while_running(self, bash: str, tmp_path: Path, unit: str, state: str) -> None:
        env = self.env(tmp_path, **{f"FAKE_STATE_{unit}": state})
        r = run_deploy_script(UPDATE, env, bash=bash)
        assert r.code == 1
        assert unit.replace("_service", ".service").replace("_", "-") + " 가 도는 중" in r.err
        lines = norm(r.lines)
        for bad in ("fetch", "merge", "pip", "backup"):
            assert not has(lines, bad), bad
        assert has(lines, "systemctl start team-wiki-sync.timer")

    def test_force_proceeds(self, bash: str, tmp_path: Path) -> None:
        env = self.env(tmp_path, FAKE_STATE_team_wiki_sync_service="active", FORCE="1")
        r = run_deploy_script(UPDATE, env, bash=bash)
        assert r.code == 0 and has(norm(r.lines), "fetch")

    def test_inactive_timer_is_left_alone(self, bash: str, tmp_path: Path) -> None:
        env = self.env(tmp_path, FAKE_STATE_team_wiki_sync_timer="inactive")
        r = run_deploy_script(UPDATE, env, bash=bash)
        assert r.code == 0
        lines = norm(r.lines)
        assert not has(lines, "systemctl stop") and not has(lines, "systemctl start")

    def test_rollback(self, bash: str, tmp_path: Path) -> None:
        env = self.env(tmp_path)
        r = run_deploy_script(UPDATE, env, args=["--rollback"], bash=bash)
        assert r.code == 1 and "되돌릴 커밋 기록이 없습니다" in r.err
        state = Path(env["DEPLOY_STATE"])
        state.mkdir(parents=True)
        (state / "engine-previous").write_text(SHA + "\n")
        r = run_deploy_script(UPDATE, env, args=["--rollback"], bash=bash)
        assert r.code == 0, r.err
        lines = norm(r.lines)
        assert has(lines, f"git -C {env['ENGINE_DIR']} reset --hard --quiet {SHA}")
        assert not has(lines, "fetch")
        for bad in ("zzz\n", SHA[:39] + "\n", "\n"):
            (state / "engine-previous").write_text(bad)
            r = run_deploy_script(UPDATE, env, args=["--rollback"], bash=bash)
            assert r.code == 1, bad

    def test_not_a_git_repo_fails(self, bash: str, tmp_path: Path) -> None:
        env = self.env(tmp_path)
        shutil.rmtree(Path(env["ENGINE_DIR"]) / ".git")
        r = run_deploy_script(UPDATE, env, bash=bash)
        assert r.code != 0
