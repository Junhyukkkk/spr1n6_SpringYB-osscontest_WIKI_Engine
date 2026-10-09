"""`team-wiki-preflight` — 서비스 시작 전에 배포 가정을 확인하는 오프라인 점검.

시간대 자료가 빠진 서버에서도 원인을 말하며 끝나야 하므로 모듈 수준 import는 표준 라이브러리,
`config.load`, `git.client`뿐이다(`util.time`·`github.client`·`metrics.record`·`claude_agent_sdk`는
부르지 않는다. 필요한 `parse_repo_url`은 함수 안에서 늦게 import한다). 각 점검은 예외를 던지지 않고,
출력에는 비밀값·프록시 주소·자격 증명을 싣지 않는다(변수 이름과 있음/없음만).
"""

import asyncio
import importlib.metadata
import importlib.util
import os
import platform
import re
import shutil
import stat
import sys
import tomllib
import unicodedata
import zoneinfo
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from team_wiki_engine.config.load import (
    ConfigError,
    Paths,
    load_config,
    resolve_paths,
)
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.git.client import GitError, check_git_version

Role = Literal["bot", "sync", "pull", "smoke"]
Status = Literal["ok", "warn", "fail", "skip"]

REQUIRED_SKILLS: Final = ("wiki-schema", "ask", "sync-code", "ingest")
NETWORK_FS: Final = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "afs", "ceph", "glusterfs", "sshfs", "fuse"}
)
PROXY_VARS: Final = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
ELF_MACHINE: Final = {"aarch64": 183, "arm64": 183, "x86_64": 62, "amd64": 62}
MIN_FREE_BYTES: Final = 5 * 2**30
USAGE = "사용법: team-wiki-preflight --role bot|sync|pull|smoke [--quiet]"

_DEFAULT_CONFIG = "/etc/team-wiki/team.json"
_ROLES: Final = ("bot", "sync", "pull", "smoke")
_MAX_READ = 64 * 1024
_MAX_PYPROJECT = 256 * 1024
_SDK_NAME = "claude-agent-sdk"
_LABEL: Final = {"ok": "정상", "warn": "주의", "fail": "실패", "skip": "건너뜀"}
_CHECK_NAMES: Final = (
    "python",
    "timezone",
    "git",
    "sdk_pin",
    "bundled_cli",
    "skills",
    "config",
    "secrets",
    "work_dirs",
    "local_fs",
    "proxy",
    "clone_symlinks",
    "disk",
)


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def check_python(version: tuple[int, int]) -> Check:
    if version >= (3, 12):
        return Check("python", "ok", f"Python {version[0]}.{version[1]}")
    return Check(
        "python", "fail", f"Python 3.12 이상이 필요합니다 (현재 {version[0]}.{version[1]})"
    )


def check_timezone() -> Check:
    try:
        zoneinfo.ZoneInfo("Asia/Seoul")
    except zoneinfo.ZoneInfoNotFoundError:
        return Check("timezone", "fail", "시간대 자료가 없습니다 — sudo apt install tzdata")
    except Exception as e:
        return Check("timezone", "fail", f"시간대를 읽을 수 없습니다 ({type(e).__name__})")
    return Check("timezone", "ok", "Asia/Seoul")


def check_git(env: Mapping[str, str], git_bin: str = "git") -> Check:
    try:
        version = asyncio.run(check_git_version(git_bin, base_env=env))
    except GitError as e:
        return Check("git", "fail", str(e))
    except OSError as e:
        return Check("git", "fail", f"git을 실행할 수 없습니다 ({type(e).__name__})")
    except Exception as e:
        return Check("git", "fail", f"git 점검 실패 ({type(e).__name__})")
    return Check("git", "ok", "git " + ".".join(str(n) for n in version))


def sdk_pin_from_pyproject(text: str) -> str | None:
    try:
        data = tomllib.loads(text)
        deps = data["project"]["dependencies"]
        if not isinstance(deps, list):
            return None
        specs: list[str] = []
        for dep in deps:
            if not isinstance(dep, str):
                continue
            m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._\-]*)", dep)
            if m and re.sub(r"[-_.]+", "-", m.group(1)).lower() == _SDK_NAME:
                specs.append(dep)
        if len(specs) != 1:
            return None
        pin = re.fullmatch(
            r"\s*claude[-_.]agent[-_.]sdk\s*==\s*([0-9]+\.[0-9]+\.[0-9]+)\s*",
            specs[0],
            re.IGNORECASE,
        )
        return pin.group(1) if pin else None
    except Exception:
        return None


def check_sdk_pin(pyproject_text: str, installed: str | None) -> Check:
    pinned = sdk_pin_from_pyproject(pyproject_text)
    if pinned is None:
        return Check("sdk_pin", "fail", f"{_SDK_NAME}가 정확히 고정되어 있지 않습니다")
    if installed is None:
        return Check("sdk_pin", "fail", f"{_SDK_NAME}가 설치되어 있지 않습니다")
    if installed != pinned:
        return Check(
            "sdk_pin",
            "fail",
            f"설치 {installed} ≠ 고정 {pinned} — 같은 버전으로 라이브 스모크를 다시 통과한 뒤 "
            "고정을 올리세요",
        )
    return Check("sdk_pin", "ok", pinned)


def elf_machine(header: bytes) -> int | None:
    if len(header) < 20 or header[:4] != b"\x7fELF":
        return None
    if header[5] == 1:
        return int.from_bytes(header[18:20], "little")
    if header[5] == 2:
        return int.from_bytes(header[18:20], "big")
    return None


def check_bundled_cli(cli: Path | None, machine: str, system: str) -> Check:
    name = "bundled_cli"
    try:
        if cli is None or not cli.is_file():
            return Check(name, "fail", "동봉 CLI를 찾을 수 없습니다")
        if not os.access(cli, os.X_OK):
            return Check(name, "fail", "동봉 CLI에 실행 권한이 없습니다")
        if system != "Linux":
            return Check(name, "skip", "리눅스가 아님 — 아키텍처 확인 생략")
        fd = os.open(cli, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as f:
            header = f.read(20)
        found = elf_machine(header)
        if found is None or found != ELF_MACHINE.get(machine):
            return Check(name, "fail", "동봉 CLI 아키텍처가 서버와 다릅니다")
        return Check(name, "ok", f"ELF {machine}")
    except Exception as e:
        return Check(name, "fail", f"동봉 CLI를 확인할 수 없습니다 ({type(e).__name__})")


def check_skills(engine_dir: Path) -> Check:
    missing: list[str] = []
    for skill in REQUIRED_SKILLS:
        try:
            ok = stat.S_ISREG(os.stat(engine_dir / "skills" / skill / "SKILL.md").st_mode)
        except OSError:
            ok = False
        if not ok:
            missing.append(skill)
    if missing:
        return Check("skills", "fail", "스킬이 없습니다: " + ", ".join(missing))
    return Check("skills", "ok", ", ".join(REQUIRED_SKILLS))


def _config_path(env: Mapping[str, str]) -> str:
    return env.get("TEAM_WIKI_CONFIG") or _DEFAULT_CONFIG


def check_config(env: Mapping[str, str], role: Role) -> tuple[Check, TeamConfig | None]:
    name = "config"
    if role == "smoke":
        return Check(name, "skip", "스모크는 설정을 쓰지 않습니다"), None
    try:
        config = load_config(_config_path(env))
    except ConfigError as e:
        return Check(name, "fail", str(e)), None
    except Exception as e:
        return Check(name, "fail", f"설정을 읽을 수 없습니다 ({type(e).__name__})"), None
    try:
        return _check_config_rules(config, role), config
    except Exception as e:
        return Check(name, "fail", f"설정 점검 실패 ({type(e).__name__})"), config


def _check_config_rules(config: TeamConfig, role: Role) -> Check:
    name = "config"
    url = config.data_repo.url
    status: Status = "ok"
    detail = f"전달 방식 {config.delivery}"
    if config.delivery == "local":
        if not os.path.isabs(url):
            return Check(name, "fail", "local 모드의 dataRepo.url은 절대 경로여야 합니다")
        if not os.path.isdir(url):
            if role in ("sync", "pull"):
                return Check(
                    name,
                    "fail",
                    "데이터 저장소가 없습니다 — deploy/init-data-repo.sh로 만드세요",
                )
            if role == "bot":
                status, detail = "warn", "데이터 저장소가 아직 없습니다"
    else:
        if not url.startswith("https://github.com/"):
            return Check(
                name,
                "fail",
                "github 모드의 dataRepo.url은 https://github.com/ 주소여야 합니다 "
                "(git@ 주소는 GIT_ALLOW_PROTOCOL=https에 막힌다)",
            )
        if "@" in url:
            return Check(name, "fail", "dataRepo.url에 자격 증명이 들어 있습니다")
        try:
            from team_wiki_engine.github.client import parse_repo_url
        except Exception:
            return Check(name, "fail", "시간대 자료가 없어 확인할 수 없습니다")
        if parse_repo_url(url) is None:
            return Check(name, "fail", "dataRepo.url을 GitHub 저장소 주소로 읽을 수 없습니다")
    if role == "bot" and (not config.discord.guild_id.strip() or not config.discord.channel_ids):
        return Check(name, "fail", "봇에는 discord.guildId와 channelIds가 필요합니다")
    return Check(name, status, detail)


def check_secrets(env: Mapping[str, str], role: Role, config: TeamConfig | None) -> Check:
    github = config is not None and config.delivery == "github"
    required: list[str] = []
    if role == "bot":
        required = ["ANTHROPIC_API_KEY", "DISCORD_TOKEN"]
    elif role == "sync":
        required = ["ANTHROPIC_API_KEY"]
    elif role == "smoke":
        required = ["ANTHROPIC_API_KEY"]
    if github and role in ("bot", "sync", "pull"):
        required.append("GITHUB_TOKEN")
    optional = ["DISCORD_TOKEN"] if role == "sync" else []

    def present(n: str) -> bool:
        return bool(env.get(n, "").strip())

    missing = [n for n in required if not present(n)]
    warn_missing = [n for n in optional if not present(n)]
    have = [n for n in (*required, *optional) if present(n)]
    parts: list[str] = []
    if have:
        parts.append("있음: " + ", ".join(have))
    if missing:
        parts.append("없음: " + ", ".join(missing))
    if warn_missing:
        parts.append("없음(알림은 표준 출력으로만 갑니다): " + ", ".join(warn_missing))
    detail = " · ".join(parts) or "필요한 비밀값 없음"
    status: Status = "fail" if missing else ("warn" if warn_missing else "ok")
    return Check("secrets", status, detail)


def _unescape_octal(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def fs_type_for(path: Path, mountinfo: str) -> str | None:
    try:
        target = os.path.realpath(path)
    except Exception:
        return None
    best_len = -1
    best: str | None = None
    for line in mountinfo.splitlines():
        try:
            head, sep, tail = line.partition(" - ")
            fields = head.split(" ")
            if not sep or len(fields) < 5:
                continue
            mount = _unescape_octal(fields[4])
            kind = tail.split(" ")[0]
            if not kind:
                continue
            prefix = mount.rstrip("/")
            if target == mount or target.startswith(prefix + "/") or mount == "/":
                if len(mount) > best_len:
                    best_len, best = len(mount), kind
        except Exception:
            continue
    return best


def _nearest_existing(path: Path) -> Path:
    current = path
    while not os.path.lexists(current) and current.parent != current:
        current = current.parent
    return current


def check_local_fs(state_dir: Path, mountinfo: str | None) -> Check:
    if mountinfo is None:
        return Check("local_fs", "skip", "리눅스가 아님")
    try:
        kind = fs_type_for(_nearest_existing(state_dir), mountinfo)
    except Exception as e:
        return Check("local_fs", "fail", f"파일시스템을 확인할 수 없습니다 ({type(e).__name__})")
    if kind is not None and (kind in NETWORK_FS or kind.startswith("fuse.")):
        return Check(
            "local_fs",
            "fail",
            f"state가 네트워크 파일시스템({kind}) 위에 있습니다 — flock 잠금이 보장되지 않습니다",
        )
    return Check("local_fs", "ok", kind or "알 수 없음")


def check_proxy(env: Mapping[str, str]) -> Check:
    found = [n for n in PROXY_VARS if env.get(n, "").strip()]
    if found:
        return Check(
            "proxy", "fail", "프록시는 지원하지 않습니다(배포 결정 G6): " + ", ".join(found)
        )
    return Check("proxy", "ok", "프록시 변수 없음")


def check_work_dirs(paths: Paths, uid: int) -> Check:
    name = "work_dirs"
    try:
        home = str(paths.home)
        if not os.path.isabs(home) or not unicodedata.is_normalized("NFC", home):
            return Check(name, "fail", "TEAM_WIKI_HOME은 NFC 절대 경로여야 합니다")
        problems: list[str] = []
        seen = 0
        for label, path, strict in (
            ("home", paths.home, False),
            ("home/home", paths.home / "home", False),
            ("state", paths.state_dir, True),
        ):
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            seen += 1
            if stat.S_ISLNK(st.st_mode):
                problems.append(f"{label}: 링크입니다")
                continue
            if st.st_uid != uid:
                problems.append(f"{label}: 소유자가 서비스 사용자가 아닙니다")
            if st.st_mode & 0o022:
                problems.append(f"{label}: 그룹·기타 쓰기 권한이 있습니다")
            if strict and st.st_mode & 0o077:
                problems.append(f"{label}: 권한이 0700이어야 합니다")
        if problems:
            return Check(name, "fail", "; ".join(problems))
        if seen == 0:
            return Check(name, "ok", "아직 없음 — tmpfiles가 만든다")
        return Check(name, "ok", "소유자·권한 정상")
    except Exception as e:
        return Check(name, "fail", f"작업 폴더를 확인할 수 없습니다 ({type(e).__name__})")


def core_symlinks_disabled(git_config_text: str) -> bool | None:
    in_core = False
    for raw in git_config_text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            m = re.match(r"\[\s*([A-Za-z0-9.\-]+)\s*\]", line)
            in_core = m is not None and m.group(1).lower() == "core"
            continue
        if not in_core:
            continue
        m2 = re.fullmatch(r"symlinks\s*=\s*([A-Za-z0-9]+)\s*(?:[#;].*)?", line, re.IGNORECASE)
        if m2:
            value = m2.group(1).lower()
            if value in ("false", "no", "off", "0"):
                return True
            if value in ("true", "yes", "on", "1"):
                return False
    return None


def _read_git_config(clone: Path) -> str | None:
    try:
        fd = os.open(clone / ".git" / "config", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            return f.read(_MAX_READ).decode("utf-8", errors="replace")
    except OSError:
        return None


def check_clone_symlinks(paths: Paths) -> Check:
    try:
        bad: list[str] = []
        found = 0
        for label, clone in (("data", paths.data_dir), ("data-sync", paths.data_sync_dir)):
            text = _read_git_config(clone)
            if text is None:
                continue
            found += 1
            if core_symlinks_disabled(text) is not True:
                bad.append(label)
        if bad:
            return Check(
                "clone_symlinks",
                "warn",
                f"core.symlinks=false가 아닙니다: {', '.join(bad)} — 다음 동기화·pull이 고친다",
            )
        return Check("clone_symlinks", "ok", "사본 없음" if found == 0 else "core.symlinks=false")
    except Exception as e:
        return Check("clone_symlinks", "fail", f"사본을 확인할 수 없습니다 ({type(e).__name__})")


def check_disk(path: Path, min_free: int = MIN_FREE_BYTES) -> Check:
    try:
        free = shutil.disk_usage(_nearest_existing(path)).free
    except Exception as e:
        return Check("disk", "fail", f"디스크를 확인할 수 없습니다 ({type(e).__name__})")
    if free < min_free:
        return Check("disk", "warn", f"여유 공간이 {free // 2**20}MiB뿐입니다")
    return Check("disk", "ok", f"여유 {free // 2**30}GiB")


def engine_dir(env: Mapping[str, str]) -> Path:
    configured = env.get("TEAM_WIKI_ENGINE_DIR", "").strip()
    if configured:
        return Path(os.path.abspath(configured))
    return Path(__file__).resolve().parents[3]


def _read_pyproject(engine: Path) -> str:
    try:
        fd = os.open(engine / "pyproject.toml", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            return f.read(_MAX_PYPROJECT).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _safe(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as e:
        return Check(name, "fail", f"점검 실패 ({type(e).__name__})")


def run_checks(
    env: Mapping[str, str],
    role: Role,
    *,
    engine: Path,
    uid: int,
    machine: str,
    system: str,
    mountinfo: str | None,
    installed_sdk: str | None,
    bundled_cli: Path | None,
    git_bin: str = "git",
) -> list[Check]:
    paths = resolve_paths(env, engine)
    smoke = role == "smoke"

    def skipped(n: str) -> Check:
        return Check(n, "skip", "스모크는 쓰지 않습니다")

    config: TeamConfig | None = None
    config_check: Check
    try:
        config_check, config = check_config(env, role)
    except Exception as e:
        config_check = Check("config", "fail", f"점검 실패 ({type(e).__name__})")
    return [
        _safe("python", lambda: check_python((sys.version_info[0], sys.version_info[1]))),
        _safe("timezone", check_timezone),
        _safe("git", lambda: check_git(env, git_bin)),
        _safe("sdk_pin", lambda: check_sdk_pin(_read_pyproject(engine), installed_sdk)),
        _safe("bundled_cli", lambda: check_bundled_cli(bundled_cli, machine, system)),
        _safe("skills", lambda: check_skills(engine)),
        config_check,
        _safe("secrets", lambda: check_secrets(env, role, config)),
        skipped("work_dirs") if smoke else _safe("work_dirs", lambda: check_work_dirs(paths, uid)),
        _safe("local_fs", lambda: check_local_fs(paths.state_dir, mountinfo)),
        _safe("proxy", lambda: check_proxy(env)),
        skipped("clone_symlinks")
        if smoke
        else _safe("clone_symlinks", lambda: check_clone_symlinks(paths)),
        _safe("disk", lambda: check_disk(paths.home)),
    ]


def _escape(text: str) -> str:
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode() for ch in text)


def format_check(c: Check) -> str:
    return f"[preflight] {_LABEL[c.status]} {_escape(c.name)}: {_escape(c.detail)}"


def _parse_args(argv: Sequence[str]) -> tuple[Role, bool] | None:
    role: str | None = None
    quiet = False
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--quiet":
            quiet = True
        elif arg == "--role" and args and role is None:
            role = args.pop(0)
        else:
            return None
    if role is None or role not in _ROLES:
        return None
    return role, quiet  # type: ignore[return-value]


def _bundled_cli_path() -> Path | None:
    try:
        spec = importlib.util.find_spec("claude_agent_sdk")
    except Exception:
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent / "_bundled" / "claude"


def _installed_sdk() -> str | None:
    try:
        return importlib.metadata.version(_SDK_NAME)
    except Exception:
        return None


def _read_mountinfo() -> str | None:
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            return f.read(4 * 1024 * 1024)
    except OSError:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parsed = _parse_args(sys.argv[1:] if argv is None else argv)
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    role, quiet = parsed
    env = os.environ
    checks = run_checks(
        env,
        role,
        engine=engine_dir(env),
        uid=os.getuid(),
        machine=platform.machine(),
        system=platform.system(),
        mountinfo=_read_mountinfo(),
        installed_sdk=_installed_sdk(),
        bundled_cli=_bundled_cli_path(),
    )
    for c in checks:
        if quiet and c.status in ("ok", "skip"):
            continue
        print(format_check(c))
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    print(f"[preflight] {role}: 실패 {fails} · 주의 {warns}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
