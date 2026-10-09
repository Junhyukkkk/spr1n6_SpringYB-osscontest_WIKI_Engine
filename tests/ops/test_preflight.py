import json
import os
import shutil
import struct
import subprocess
import sys
import zoneinfo
from pathlib import Path

import pytest

from team_wiki_engine.config.load import Paths, resolve_paths
from team_wiki_engine.ops import preflight as pf
from team_wiki_engine.ops.preflight import (
    Check,
    check_bundled_cli,
    check_clone_symlinks,
    check_config,
    check_git,
    check_local_fs,
    check_proxy,
    check_python,
    check_sdk_pin,
    check_secrets,
    check_skills,
    check_timezone,
    check_work_dirs,
    core_symlinks_disabled,
    elf_machine,
    engine_dir,
    format_check,
    fs_type_for,
    main,
    run_checks,
    sdk_pin_from_pyproject,
)
from tests.helpers.fake_git import write_fake_git

REPO = Path(__file__).resolve().parents[2]
SENTINEL = "sk-ant-TESTSENTINEL0123456789"


def elf(machine: int, *, big: bool = False) -> bytes:
    ident = b"\x7fELF" + bytes([2, 2 if big else 1, 1]) + b"\x00" * 9
    return ident + struct.pack(">HH" if big else "<HH", 2, machine)


def config_file(tmp_path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "repos": [{"name": "server", "url": "https://github.com/o/server.git"}],
        "dataRepo": {"url": str(tmp_path / "data.git")},
        "discord": {"guildId": "1", "channelIds": ["2"]},
    }
    data.update(overrides)
    path = tmp_path / "team.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_python() -> None:
    assert check_python((3, 11)).status == "fail"
    assert check_python((3, 12)).status == "ok"
    assert check_python((3, 14)).status == "ok"


def test_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    assert check_timezone().status == "ok"

    def boom(key: str) -> object:
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", boom)
    c = check_timezone()
    assert c.status == "fail"
    assert "tzdata" in c.detail


def test_git(tmp_path: Path) -> None:
    old = write_fake_git(tmp_path / "old", version="git version 2.31.0")
    new = write_fake_git(tmp_path / "new", version="git version 2.43.0")
    assert check_git(dict(os.environ), str(old)).status == "fail"
    assert check_git(dict(os.environ), str(new)).status == "ok"
    assert check_git(dict(os.environ), str(tmp_path / "nope")).status == "fail"


def test_sdk_pin_파싱() -> None:
    assert sdk_pin_from_pyproject((REPO / "pyproject.toml").read_text()) == "0.2.164"

    def toml(*deps: str) -> str:
        return "[project]\ndependencies = [" + ", ".join(f'"{d}"' for d in deps) + "]\n"

    assert sdk_pin_from_pyproject(toml("claude-agent-sdk>=0.2.164")) is None
    assert sdk_pin_from_pyproject(toml("claude-agent-sdk~=0.2")) is None
    assert (
        sdk_pin_from_pyproject(toml("claude-agent-sdk==1.0.0", "claude_agent_sdk==1.0.1")) is None
    )
    assert sdk_pin_from_pyproject(toml("pyyaml")) is None
    assert sdk_pin_from_pyproject("[project\nbroken") is None
    assert sdk_pin_from_pyproject(toml("claude-agent-sdk==1.2.3")) == "1.2.3"


def test_sdk_pin_점검() -> None:
    text = (REPO / "pyproject.toml").read_text()
    assert check_sdk_pin(text, "0.2.164").status == "ok"
    different = check_sdk_pin(text, "0.2.165")
    assert different.status == "fail"
    assert "라이브 스모크" in different.detail
    assert check_sdk_pin(text, None).status == "fail"
    assert check_sdk_pin("", "0.2.164").status == "fail"


def test_elf_machine() -> None:
    assert elf_machine(elf(183)) == 183
    assert elf_machine(elf(62, big=True)) == 62
    assert elf_machine(b"\xcf\xfa\xed\xfe" + b"\x00" * 30) is None
    assert elf_machine(b"\x7fELF\x02\x01") is None
    assert elf_machine(b"") is None


def test_bundled_cli(tmp_path: Path) -> None:
    cli = tmp_path / "claude"
    cli.write_bytes(elf(183))
    cli.chmod(0o755)
    assert check_bundled_cli(cli, "aarch64", "Linux").status == "ok"
    assert check_bundled_cli(cli, "x86_64", "Linux").status == "fail"
    assert check_bundled_cli(cli, "aarch64", "Darwin").status == "skip"
    cli.chmod(0o644)
    assert check_bundled_cli(cli, "aarch64", "Linux").status == "fail"
    assert check_bundled_cli(None, "aarch64", "Linux").status == "fail"
    assert check_bundled_cli(tmp_path / "none", "aarch64", "Linux").status == "fail"


def test_skills(tmp_path: Path) -> None:
    assert check_skills(REPO).status == "ok"
    copy = tmp_path / "engine"
    shutil.copytree(REPO / "skills", copy / "skills")
    shutil.rmtree(copy / "skills" / "ingest")
    c = check_skills(copy)
    assert c.status == "fail"
    assert "ingest" in c.detail


class TestConfig:
    def env(self, path: Path) -> dict[str, str]:
        return {"TEAM_WIKI_CONFIG": str(path)}

    def test_local_상대_경로(self, tmp_path: Path) -> None:
        p = config_file(tmp_path, dataRepo={"url": "data.git"})
        assert check_config(self.env(p), "sync")[0].status == "fail"

    def test_local_없는_절대_경로(self, tmp_path: Path) -> None:
        p = config_file(tmp_path)
        assert check_config(self.env(p), "sync")[0].status == "fail"
        assert check_config(self.env(p), "pull")[0].status == "fail"
        assert check_config(self.env(p), "bot")[0].status == "warn"
        (tmp_path / "data.git").mkdir()
        assert check_config(self.env(p), "sync")[0].status == "ok"

    def test_github(self, tmp_path: Path) -> None:
        def status(url: str) -> Check:
            p = config_file(tmp_path, delivery="github", dataRepo={"url": url})
            return check_config(self.env(p), "sync")[0]

        assert status("git@github.com:o/r.git").status == "fail"
        cred = status("https://x-access-token:abc@github.com/o/r.git")
        assert cred.status == "fail"
        assert "abc" not in cred.detail
        assert status("https://github.com/your-org/team-wiki-data.git").status == "ok"

    def test_bot_디스코드(self, tmp_path: Path) -> None:
        p = config_file(tmp_path, discord={"guildId": "", "channelIds": ["2"]})
        (tmp_path / "data.git").mkdir()
        assert check_config(self.env(p), "bot")[0].status == "fail"
        p2 = config_file(tmp_path, discord={"guildId": "1", "channelIds": []})
        assert check_config(self.env(p2), "bot")[0].status == "fail"

    def test_깨진_json과_스모크(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{nope")
        c, cfg = check_config(self.env(p), "sync")
        assert c.status == "fail" and cfg is None
        assert check_config(self.env(p), "smoke")[0].status == "skip"


class TestSecrets:
    @pytest.mark.parametrize(
        ("role", "github", "names"),
        [
            ("bot", False, {"ANTHROPIC_API_KEY", "DISCORD_TOKEN"}),
            ("bot", True, {"ANTHROPIC_API_KEY", "DISCORD_TOKEN", "GITHUB_TOKEN"}),
            ("sync", False, {"ANTHROPIC_API_KEY"}),
            ("sync", True, {"ANTHROPIC_API_KEY", "GITHUB_TOKEN"}),
            ("pull", False, set()),
            ("pull", True, {"GITHUB_TOKEN"}),
            ("smoke", False, {"ANTHROPIC_API_KEY"}),
        ],
    )
    def test_필요_목록(self, role: str, github: bool, names: set[str], tmp_path: Path) -> None:
        p = config_file(tmp_path, delivery="github" if github else "local")
        from team_wiki_engine.config.load import load_config

        cfg = load_config(p)
        full = {n: "v" * 12 for n in names}
        assert check_secrets(full, role, cfg).status in ("ok", "warn")  # type: ignore[arg-type]
        for n in names:
            partial = {k: v for k, v in full.items() if k != n}
            assert check_secrets(partial, role, cfg).status == "fail"  # type: ignore[arg-type]

    def test_sync는_디스코드_없으면_주의(self) -> None:
        c = check_secrets({"ANTHROPIC_API_KEY": "x" * 12}, "sync", None)
        assert c.status == "warn"

    def test_값_비노출과_공백(self) -> None:
        c = check_secrets({"ANTHROPIC_API_KEY": SENTINEL, "DISCORD_TOKEN": "   "}, "bot", None)
        assert SENTINEL not in c.detail
        assert c.status == "fail"
        assert "DISCORD_TOKEN" in c.detail


MOUNTINFO = (
    "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
    "garbage line\n"
    "30 22 0:40 / /srv rw - nfs4 server:/x rw\n"
    "31 30 8:2 / /srv/team-wiki\\040x rw - xfs /dev/sdb rw\n"
    "32 22 0:41 / /mnt/f rw - fuse.sshfs u@h:/ rw\n"
)


def test_fs_type_for() -> None:
    assert fs_type_for(Path("/srv/a"), MOUNTINFO) == "nfs4"
    assert fs_type_for(Path("/srv/team-wiki x/state"), MOUNTINFO) == "xfs"
    assert fs_type_for(Path("/opt"), MOUNTINFO) == "ext4"
    assert fs_type_for(Path("/opt"), "x\n- -\n") is None


def test_local_fs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pf, "_nearest_existing", lambda p: p)  # 예시 경로는 이 머신에 없다
    assert check_local_fs(Path("/srv/a/state"), MOUNTINFO).status == "fail"
    assert check_local_fs(Path("/mnt/f/state"), MOUNTINFO).status == "fail"
    assert check_local_fs(Path("/opt/state"), MOUNTINFO).status == "ok"
    assert check_local_fs(Path("/opt/state"), None).status == "skip"


def test_proxy() -> None:
    c = check_proxy({"https_proxy": "http://p:3128"})
    assert c.status == "fail"
    assert "p:3128" not in c.detail
    assert "https_proxy" in c.detail
    assert check_proxy({"https_proxy": ""}).status == "ok"


class TestWorkDirs:
    def paths(self, home: Path) -> Paths:
        return resolve_paths({"TEAM_WIKI_HOME": str(home)}, REPO)

    def test_정상과_없음(self, tmp_path: Path) -> None:
        home = tmp_path / "h"
        home.mkdir(mode=0o750)
        home.chmod(0o750)
        state = home / "state"
        state.mkdir(mode=0o700)
        assert check_work_dirs(self.paths(home), os.getuid()).status == "ok"
        assert check_work_dirs(self.paths(tmp_path / "none"), os.getuid()).status == "ok"

    def test_나쁜_권한(self, tmp_path: Path) -> None:
        home = tmp_path / "h"
        home.mkdir()
        home.chmod(0o750)
        state = home / "state"
        state.mkdir()
        state.chmod(0o755)
        assert check_work_dirs(self.paths(home), os.getuid()).status == "fail"
        state.chmod(0o700)
        home.chmod(0o770)
        assert check_work_dirs(self.paths(home), os.getuid()).status == "fail"

    def test_링크와_다른_소유자(self, tmp_path: Path) -> None:
        home = tmp_path / "h"
        home.mkdir()
        home.chmod(0o750)
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        os.symlink(real, home / "state")
        assert check_work_dirs(self.paths(home), os.getuid()).status == "fail"
        assert check_work_dirs(self.paths(home), os.getuid() + 1).status == "fail"

    def test_nfd(self, tmp_path: Path) -> None:
        import unicodedata

        nfd = unicodedata.normalize("NFD", str(tmp_path / "한글"))
        assert check_work_dirs(self.paths(Path(nfd)), os.getuid()).status == "fail"


def test_core_symlinks() -> None:
    assert core_symlinks_disabled("[core]\n\tsymlinks = false") is True
    assert core_symlinks_disabled("[Core]\nsymlinks=No") is True
    assert core_symlinks_disabled("[core]\nsymlinks = true") is False
    assert core_symlinks_disabled("[other]\nsymlinks=false") is None
    assert core_symlinks_disabled("") is None


def test_clone_symlinks(tmp_path: Path) -> None:
    paths = resolve_paths({"TEAM_WIKI_HOME": str(tmp_path)}, REPO)
    assert check_clone_symlinks(paths).status == "ok"
    (paths.data_dir / ".git").mkdir(parents=True)
    (paths.data_dir / ".git" / "config").write_text("[core]\nsymlinks = true\n")
    assert check_clone_symlinks(paths).status == "warn"
    (paths.data_dir / ".git" / "config").write_text("[core]\nsymlinks = false\n")
    assert check_clone_symlinks(paths).status == "ok"


def test_disk(tmp_path: Path) -> None:
    assert pf.check_disk(tmp_path / "x" / "y", 1).status == "ok"
    assert pf.check_disk(tmp_path, 2**62).status == "warn"


def test_engine_dir_같은_규칙() -> None:
    sync_main = pytest.importorskip("team_wiki_engine.sync.main")  # 계획 2 Task 17 이후
    for env in ({}, {"TEAM_WIKI_ENGINE_DIR": "  "}, {"TEAM_WIKI_ENGINE_DIR": "/opt/x"}):
        assert engine_dir(env) == sync_main.engine_dir_from(env)


def test_engine_dir_규칙_자체() -> None:
    assert engine_dir({"TEAM_WIKI_ENGINE_DIR": " /opt/x "}) == Path("/opt/x")
    assert engine_dir({}) == REPO


def run_all(tmp_path: Path, role: str) -> list[Check]:
    env = {"TEAM_WIKI_CONFIG": str(config_file(tmp_path)), "TEAM_WIKI_HOME": str(tmp_path / "h")}
    return run_checks(
        env,
        role,  # type: ignore[arg-type]
        engine=REPO,
        uid=os.getuid(),
        machine="aarch64",
        system="Darwin",
        mountinfo=None,
        installed_sdk="0.2.164",
        bundled_cli=None,
        git_bin=str(write_fake_git(tmp_path / "g", version="git version 2.43.0")),
    )


NAMES = [
    "python", "timezone", "git", "sdk_pin", "bundled_cli", "skills", "config", "secrets",
    "work_dirs", "local_fs", "proxy", "clone_symlinks", "disk",
]  # fmt: skip


def test_run_checks_순서와_스모크(tmp_path: Path) -> None:
    assert [c.name for c in run_all(tmp_path, "sync")] == NAMES
    by_name = {c.name: c for c in run_all(tmp_path, "smoke")}
    for n in ("config", "work_dirs", "clone_symlinks"):
        assert by_name[n].status == "skip"


def test_format_check_제어문자() -> None:
    line = format_check(Check("x", "warn", "a\x1b[31mb\nc"))
    assert line.startswith("[preflight] 주의 x: ")
    assert "\x1b" not in line and "\n" not in line


def test_main_사용_오류() -> None:
    assert main([]) == 2
    assert main(["--role", "x"]) == 2
    assert main(["--help"]) == 2


def test_main_비밀값_비노출(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_TESTSENTINELGITHUB0123456789")
    monkeypatch.setenv("TEAM_WIKI_CONFIG", str(config_file(tmp_path)))
    monkeypatch.setenv("TEAM_WIKI_HOME", str(tmp_path / "h"))
    code = main(["--role", "sync"])
    out = capsys.readouterr()
    assert code in (0, 1)
    assert SENTINEL not in out.out + out.err
    assert "ghp_TESTSENTINEL" not in out.out + out.err
    assert out.out.strip().splitlines()[-1].startswith("[preflight] sync: 실패 ")


def test_main_quiet와_종료_코드(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TEAM_WIKI_CONFIG", str(tmp_path / "none.json"))
    monkeypatch.setenv("TEAM_WIKI_HOME", str(tmp_path / "h"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["--role", "sync", "--quiet"]) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert all("정상" not in ln and "건너뜀" not in ln for ln in lines)
    assert lines[-1].startswith("[preflight] sync: 실패 ")


def test_G15_금지_모듈을_부르지_않는다() -> None:
    code = (
        "import sys, team_wiki_engine.ops.preflight; "
        "print([m for m in ('team_wiki_engine.util.time', 'team_wiki_engine.github.client', "
        "'claude_agent_sdk') if m in sys.modules])"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.stdout.strip() == "[]", result.stderr
